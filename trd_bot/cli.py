"""CLI for synthetic demo, public Binance paper trading and recorded replay."""
import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import random
import time

from .core import Config, Engine, atomic_json

LOG = logging.getLogger("trd_bot")


@contextmanager
def runtime_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "engine.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, str(os.getpid()).encode())
        yield
    finally:
        os.close(descriptor)
        path.unlink()


def demo_events(count=2000, seed=7):
    """Explicit synthetic mechanics fixture, never historical market data."""
    rng = random.Random(seed)
    price = 60000.0
    epoch = 1767225600000
    for index in range(count):
        now = epoch + index * 100
        price = max(100.0, price + rng.gauss(0, 8))
        yield {"type": "book", "received_ms": now, "timestamp": now,
            "bids": [[f"{price - 1:.2f}", "0.002"]],
            "asks": [[f"{price + 1:.2f}", "0.002"]]}
        yield {"type": "trade", "received_ms": now + 70, "timestamp": now + 60,
            "id": str(index), "side": "buy" if index % 2 else "sell",
            "price": f"{price + (40 if index % 2 else -40):.2f}", "amount": "0.003"}


def replay(engine, events, recording=None):
    try:
        for event in events:
            if (engine.directory / "KILL").exists():
                engine.halt("kill_switch")
            if engine.halt_reason:
                break
            if recording:
                recording.write(json.dumps(event) + "\n")
            engine.process(event)
            if (engine.directory / "KILL").exists():
                engine.halt("kill_switch")
            if engine.halt_reason:
                break
    except Exception:
        engine.errors += 1
        engine.halt("invalid_replay_or_io_error")
        LOG.exception("Replay failed")
        raise
    finally:
        engine.orders.clear()
        engine.save()
        engine.health()


async def paper(engine, seconds):
    import ccxt.pro as ccxtpro
    exchange = ccxtpro.binance({"enableRateLimit": True, "newUpdates": True})
    queue = asyncio.Queue(maxsize=10000)
    tasks = []
    started = time.monotonic()
    record = None

    async def books():
        seen_timestamped_book = False
        warned_snapshot = False
        while True:
            book = await exchange.watch_order_book(engine.cfg.symbol, 20)
            if book.get("timestamp") is None and not seen_timestamped_book:
                if not warned_snapshot:
                    LOG.warning("Ignoring initial untimestamped REST snapshot; awaiting WebSocket update")
                    warned_snapshot = True
                continue
            seen_timestamped_book = True
            await queue.put({"type": "book", "received_ms": int(time.time() * 1000),
                "timestamp": book.get("timestamp"), "bids": [list(x[:2]) for x in book["bids"]],
                "asks": [list(x[:2]) for x in book["asks"]]})

    async def trades():
        while True:
            batch = await exchange.watch_trades(engine.cfg.symbol)
            for trade in batch:
                await queue.put({"type": "trade", "received_ms": int(time.time() * 1000),
                    **{key: trade.get(key) for key in ("timestamp", "id", "side", "price", "amount")}})

    try:
        if engine.halt_reason:
            return
        await asyncio.wait_for(exchange.load_markets(), 30)
        if not all(exchange.has.get(key) for key in ("watchOrderBook", "watchTrades")):
            raise RuntimeError("Required public streams unsupported")
        record = (engine.directory / "events.jsonl").open("a", encoding="utf-8", buffering=1)
        tasks = [asyncio.create_task(books()), asyncio.create_task(trades())]
        started = time.monotonic()
        while not engine.halt_reason and (seconds == 0 or time.monotonic() - started < seconds):
            for task in tasks:
                if task.done():
                    task.result()
                    raise RuntimeError("Market stream ended")
            now = int(time.time() * 1000)
            if not engine.guard(now, (engine.directory / "KILL").exists()):
                break
            if engine.last_book_ms is None and time.monotonic() - started > 30:
                engine.halt("initial_book_timeout")
                break
            try:
                event = await asyncio.wait_for(queue.get(), .1)
            except asyncio.TimeoutError:
                engine.health()
                continue
            now = int(time.time() * 1000)
            # Both source age and local backlog age are checked before any possible fill.
            if now - event["received_ms"] > engine.cfg.max_data_age_ms:
                engine.halt("feed_backlog")
                break
            record.write(json.dumps(event) + "\n")
            engine.process(event)
            engine.health()
    except asyncio.CancelledError:
        engine.halt("operator_interrupt")
        raise
    except Exception:
        engine.errors += 1
        engine.halt("feed_or_io_error")
        LOG.exception("Public feed failed; paper orders cleared")
        raise
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await exchange.close()
        finally:
            if record:
                record.close()
            engine.orders.clear()
            engine.save()
            engine.health()


def main():
    parser = argparse.ArgumentParser(description="Paper-only Binance BTC/USDT market maker")
    parser.add_argument("command", choices=("demo", "replay", "paper", "report", "resume"))
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--runtime", type=Path, default=Path("runtime/paper"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--steps", type=int, default=2000)
    parser.add_argument("--seconds", type=float, default=60, help="paper duration; 0 means continuous")
    parser.add_argument("--acknowledge-risk", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.seconds < 0 or args.steps < 1:
        parser.error("seconds must be nonnegative and steps positive")
    if args.command == "report":
        print((args.runtime / "health.json").read_text(encoding="utf-8"))
        return
    cfg = Config.load(args.config)
    with runtime_lock(args.runtime):
        if args.command == "resume":
            if not args.acknowledge_risk:
                parser.error("resume requires --acknowledge-risk after reviewing state/logs")
            if (args.runtime / "KILL").exists():
                parser.error("Remove KILL after reviewing the incident")
            state_path = args.runtime / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state["source"] != "binance_public":
                parser.error("Demo/replay must use a fresh runtime; event cursor resume is unsupported")
            engine = Engine(cfg, args.runtime, state["source"])
            # Do not reset account, daily loss baseline or high-water mark.
            engine.halt_reason = None
            engine.save()
            atomic_json(args.runtime / "resume.json", {"approved_ms": int(time.time() * 1000)})
            LOG.warning("Paper resume approved; all risk baselines preserved")
            return
        source = {"demo": "synthetic_demo", "replay": "recorded_replay", "paper": "binance_public"}[args.command]
        engine = Engine(cfg, args.runtime, source)
        # A one-use approval allows a reviewed restart; halts always win.
        approval = args.runtime / "resume.json"
        if approval.exists():
            if engine.halt_reason == "restart_requires_review":
                engine.halt_reason = None
            approval.unlink()
        if engine.halt_reason:
            engine.save()
            engine.health()
            print(json.dumps(engine.report(), indent=2))
            return
        if args.command == "paper":
            asyncio.run(paper(engine, args.seconds))
        elif args.command == "demo":
            with (args.runtime / "events.jsonl").open("w", encoding="utf-8") as output:
                replay(engine, demo_events(args.steps), output)
        else:
            if args.input is None:
                parser.error("replay requires --input events.jsonl")
            with args.input.open(encoding="utf-8") as source_file:
                replay(engine, (json.loads(line) for line in source_file if line.strip()))
        print(json.dumps(engine.report(), indent=2))


if __name__ == "__main__":
    main()
