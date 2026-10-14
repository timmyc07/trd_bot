"""Finite capture, deterministic replay and a watchdog independent of SQLite I/O."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
from pathlib import Path
import random
import time

from .core import atomic_json
from .data import AVG_URL, AllowlistedTransport, MarketRules, fetch_rules, public_events
from .research_engine import HaltSignal, ResearchEngine
from .report import compare_results, export_report, split_events

LOG = logging.getLogger("trd_bot")


def demo_events(steps=600, seed=7):
    rng = random.Random(seed)
    epoch = MarketRules.fixture().fetched_ms
    price = 60000
    for index in range(steps):
        now = epoch + index * 250
        price = max(10000, price + rng.randint(-8, 8))
        bullish = (index // 40) % 2 == 0
        common = {"received_ms": now, "timestamp": now, "source": "synthetic_demo", "connection": "connected"}
        yield {**common, "type": "book", "id": f"book:{index}",
            "bids": [[str(price-1), "0.00003" if bullish else "0.00001"]],
            "asks": [[str(price+1), "0.00001" if bullish else "0.00003"]]}
        yield {**common, "received_ms": now+150, "timestamp": now+140, "type": "trade", "id": f"trade:{index}",
            "side": "sell" if bullish else "buy", "price": str(price-20 if bullish else price+20), "amount": "0.00030"}


def publish(engine, coverage=None):
    summary = engine.report()
    state = engine.state
    coverage = coverage or {"data_kind": state.get("origin", engine.source),
        "start_ms": state.get("session_start_ms", state["coverage_start_ms"]),
        "end_ms": state.get("session_end_ms", state["coverage_end_ms"]),
        "events": state.get("session_events", state["events"]),
        "cumulative_events": state["events"], "gap_report": [row for row in engine.ledger.audit()
            if row["kind"] in ("invalid_data", "halt")], "recording_bounded": True}
    audit = engine.ledger.audit()
    report = export_report(engine.directory, summary, engine.ledger.fills(), audit,
                           [row for row in audit if row["kind"] == "equity"], coverage)
    health = dict(summary)
    health["heartbeat_utc"] = datetime.now(timezone.utc).isoformat()
    atomic_json(engine.directory / "health.json", health)
    # Diagnostic journal is rebuilt from the authoritative transaction journal.
    with (engine.directory / "fills.jsonl").open("w", encoding="utf-8") as handle:
        for fill in report["trades"]:
            handle.write(json.dumps(fill, sort_keys=True) + "\n")
    return report


def write_health(engine):
    summary = engine.report()
    summary["heartbeat_utc"] = datetime.now(timezone.utc).isoformat()
    atomic_json(engine.directory / "health.json", summary)


class Recorder:
    def __init__(self, directory, cfg, session=None):
        self.directory, self.cfg = Path(directory), cfg
        self.path = self.directory / (f"events-{session}.jsonl" if session else "events.jsonl")
        self.used = self.path.stat().st_size if self.path.exists() else 0
        self.handle = None

    def __enter__(self):
        # No deletion of research history to make space; stop and ask for archival instead.
        self.total = sum(path.stat().st_size for pattern in ("events*.jsonl", "*.initial.json")
                         for path in self.directory.parent.rglob(pattern))
        if self.used >= self.cfg.recording_max_bytes or self.total >= self.cfg.retention_max_bytes:
            raise ValueError("recording_capacity")
        self.handle = self.path.open("a", encoding="utf-8", buffering=1)
        return self

    def write(self, event):
        line = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        size = len(line.encode("utf-8"))
        if self.used + size > self.cfg.recording_max_bytes or self.total + size > self.cfg.retention_max_bytes:
            raise ValueError("recording_capacity")
        self.handle.write(line)
        self.used += size
        self.total += size

    def initial(self, snapshot):
        value = json.dumps(snapshot, indent=2, default=str)
        size = len(value.encode("utf-8"))
        if size > self.cfg.recording_max_bytes or self.total + size > self.cfg.retention_max_bytes:
            raise ValueError("recording_capacity")
        atomic_json(self.path.with_suffix(".initial.json"), snapshot)
        self.total += self.path.with_suffix(".initial.json").stat().st_size

    def __exit__(self, *args):
        if self.handle:
            self.handle.close()


def run_replay(cfg, directory, events, rules=None, source="recorded_replay", record=False, seed=None):
    engine = ResearchEngine(cfg, directory, source, rules or MarketRules.fixture(), seed=seed)
    recorder = None
    try:
        if engine.state["halt_reason"]:
            return publish(engine)
        recorder = Recorder(directory, cfg).__enter__() if record else None
        if record:
            atomic_json(Path(directory) / "rules.json", engine.state["rules"])
        for event in events:
            if (Path(directory) / "KILL").exists():
                engine.control("kill_switch")
                break
            if recorder:
                recorder.write(event)
            engine.process(event)
            if engine.state["halt_reason"]:
                break
        engine.control("completed")
        return publish(engine)
    except Exception:
        LOG.exception("Research replay failed")
        engine.control("replay_or_storage_error")
        publish(engine)
        raise
    finally:
        if recorder:
            recorder.__exit__()
        engine.close()


async def watch_silence(cfg, signal, last_seen, stop, monotonic=time.monotonic):
    """Only a latch is set here; never wait on a full data queue or synchronous I/O."""
    while not stop.is_set() and signal.reason is None:
        if monotonic() - last_seen[0] > cfg.max_data_age_ms / 1000:
            signal.request("stale_market_data")
            return
        await asyncio.sleep(.05)


async def run_public(cfg, directory, seconds=None, transport_factory=AllowlistedTransport, reset=False):
    seconds = seconds if seconds is not None else cfg.duration_seconds
    if not 0 < seconds <= 3600:
        raise ValueError("Public capture must be finite and between0 and3600 seconds")
    directory = Path(directory)
    signal, stop = HaltSignal(), asyncio.Event()
    last_seen = [time.monotonic()]
    queue = asyncio.Queue(maxsize=cfg.queue_size)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="research-ledger")
    loop = asyncio.get_running_loop()
    async def owned(function, *args):
        return await loop.run_in_executor(executor, function, *args)
    engine = await owned(lambda: ResearchEngine(cfg, directory, "binance_public", signal=signal))
    tasks, recorder = [], None
    deadline = time.monotonic() + seconds
    collected_reset_book = None
    current_rules = None

    def emit(event):
        nonlocal collected_reset_book
        # Sequence at the callback boundary, keeping the earlier transport timestamp too.
        event = {**event, "transport_received_ms": event["received_ms"], "received_ms": int(time.time()*1000)}
        if event["type"] == "book":
            last_seen[0] = time.monotonic()
            collected_reset_book = event
        if signal.reason:
            return
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            signal.request("event_queue_overflow")
            raise

    async def refresh_average(transport):
        nonlocal current_rules
        while not stop.is_set():
            await asyncio.sleep(2)
            average = await transport.get_json(AVG_URL)
            envelope = current_rules.to_payload()
            envelope["avg_price"] = {"price": average["price"], "mins": average["mins"], "timestamp": average["closeTime"]}
            current_rules = MarketRules.from_payload(envelope)
            now = int(time.time()*1000)
            emit({"type": "rules", "id": f"rules:{now}", "received_ms": now, "timestamp": now,
                "source": "binance_public", "connection": "connected", "rules": envelope})

    try:
        if not reset:
            await owned(engine.consume_reset)
            if engine.state["halt_reason"]:
                return await owned(publish, engine)
        tasks.append(asyncio.create_task(watch_silence(cfg, signal, last_seen, stop)))
        async with transport_factory() as transport:
            transport.max_data_age_ms = cfg.max_data_age_ms
            setup = asyncio.create_task(fetch_rules(transport, int(time.time()*1000)))
            tasks.append(setup)
            while not setup.done() and not signal.reason:
                if (directory / "KILL").exists() and not reset:
                    signal.request("kill_switch")
                await asyncio.sleep(.05)
            if signal.reason:
                return await owned(_finish_public, engine, signal.reason)
            current_rules = setup.result()
            current_rules.envelope["max_age_ms"] = cfg.rules_max_age_ms
            current_rules = MarketRules.from_payload(current_rules.to_payload())
            await owned(atomic_json, directory / "rules.json", current_rules.to_payload())
            tasks.append(asyncio.create_task(public_events(transport, emit)))
            tasks.append(asyncio.create_task(refresh_average(transport)))
            if reset:
                # A halted account cannot trade during reset's independent data check.
                while collected_reset_book is None and not signal.reason:
                    for task in tasks:
                        if task.done():
                            task.result()
                    await asyncio.sleep(.05)
                if signal.reason:
                    raise ValueError("Reset could not validate fresh market data")
                if int(time.time()*1000) - collected_reset_book["received_ms"] > cfg.max_data_age_ms:
                    raise ValueError("Reset market data became stale")
                await owned(engine.reset, current_rules, collected_reset_book)
                for task in tasks:
                    if task.done():
                        task.result()
                if signal.reason:
                    return await owned(_finish_public, engine, signal.reason)
                return await owned(publish, engine)
            now = int(time.time()*1000)
            first_rules = {"type": "rules", "id": f"rules:{now}", "received_ms": now, "timestamp": now,
                "source": "binance_public", "connection": "connected", "rules": current_rules.to_payload()}
            session = str(await owned(engine.ledger.event_count))
            recorder = await owned(lambda: Recorder(directory, cfg, session).__enter__())
            await owned(recorder.initial, await owned(engine.ledger.snapshot))
            await owned(recorder.write, first_rules)
            await owned(engine.process, first_rules)
            await owned(write_health, engine)
            while not signal.reason and time.monotonic() < deadline:
                if (directory / "KILL").exists():
                    signal.request("kill_switch")
                    break
                for task in tasks:
                    if task.done():
                        task.result()
                try:
                    event = await asyncio.wait_for(queue.get(), .05)
                except asyncio.TimeoutError:
                    continue
                if int(time.time()*1000) - event["received_ms"] > cfg.max_data_age_ms:
                    signal.request("feed_backlog")
                    break
                await owned(recorder.write, event)
                await owned(engine.process, event)
                if engine.state["halt_reason"]:
                    signal.request(engine.state["halt_reason"])
                if event["type"] == "book":
                    await owned(write_health, engine)
            reason = signal.reason or "duration_complete"
            control = {"type": "control", "id": f"stop:{session}:{reason}", "reason": reason,
                "timestamp": int(time.time()*1000), "received_ms": int(time.time()*1000),
                "source": "binance_public", "connection": "disconnected"}
            # Capture final stop where capacity allows; gap is still disclosed in the SQL audit.
            try:
                await owned(recorder.write, control)
            except ValueError:
                pass
            await owned(engine.process, control)
            return await owned(publish, engine)
    except asyncio.CancelledError:
        signal.request("operator_interrupt")
        await owned(_finish_public, engine, signal.reason)
        raise
    except Exception as exc:
        signal.request("recording_capacity" if str(exc) == "recording_capacity" else "public_feed_error")
        LOG.exception("Research public run stopped")
        return await owned(_finish_public, engine, signal.reason)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if recorder:
            await owned(recorder.__exit__)
        await owned(engine.close)
        executor.shutdown(wait=True)


def _finish_public(engine, reason):
    engine.control(reason)
    return publish(engine)


def comparison(cfg, directory, events, rules):
    directory = Path(directory)
    development, holdout = split_events(events)
    variants = {"no_trade": replace(cfg, strategy="no_trade"), "baseline": cfg,
        "higher_fee": replace(cfg, fee_bps=cfg.fee_bps*2),
        "conservative": replace(cfg, latency_ms=max(250,cfg.latency_ms*2),
            slippage_bps=max(cfg.slippage_bps*2, Decimal("2")), participation=cfg.participation/2, queue_multiplier=cfg.queue_multiplier*2)}
    sections = {}
    for label, partition in (("development", development), ("holdout", holdout)):
        results = {name: run_replay(variant, directory / label / name, partition, rules)
                   for name, variant in variants.items()}
        sections[label] = compare_results(results)
    result = {"split": {"development_events": len(development), "holdout_events": len(holdout),
              "parameter_tuning": False}, "results": sections}
    atomic_json(directory / "comparison.json", result)
    return result
