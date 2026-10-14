"""Primary native CLI for the versioned public-only research MVP."""
import argparse
import asyncio
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path

from .research_config import ResearchConfig
from .research_runner import comparison, demo_events, run_public, run_replay
from .data import MarketRules

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


def main():
    parser = argparse.ArgumentParser(description="Binance spot paper-only research, schema2")
    parser.add_argument("command", choices=("demo", "replay", "paper", "report", "reset", "resume", "compare", "dashboard"))
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--runtime", type=Path, default=Path("runtime/v2-paper"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--rules", type=Path)
    parser.add_argument("--steps", type=int, default=600)
    parser.add_argument("--seconds", type=float)
    parser.add_argument("--acknowledge-risk", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "dashboard":
        from .dashboard import serve
        serve(args.port, args.open_browser)
        return
    if args.command == "report":
        print((args.runtime / "report.json").read_text(encoding="utf-8"))
        return
    cfg = ResearchConfig.load(args.config)
    if args.steps < 1 or args.steps > 100000 or (args.seconds is not None and not 0 < args.seconds <= 3600):
        parser.error("steps must be1..100000; public seconds must be finite and1..3600 (zero is forbidden)")
    reset = args.command in ("reset", "resume")
    if reset and (not args.acknowledge_risk or not (args.runtime / "ledger.sqlite3").exists()):
        parser.error("reset requires existing SQLite account and --acknowledge-risk")
    if args.command in ("demo", "replay", "compare") and args.runtime.exists() and any(
            path.name != "panel.log" for path in args.runtime.iterdir()):
        parser.error("demo/replay/compare require a fresh runtime directory")
    events, rules, seed = None, None, None
    if args.command in ("replay", "compare"):
        if not args.input:
            parser.error("--input events.jsonl required")
        if args.input.stat().st_size > cfg.recording_max_bytes:
            parser.error("input exceeds configured recording capacity")
        with args.input.open(encoding="utf-8") as handle:
            events = [json.loads(line) for line in handle if line.strip()]
        if len(events) > 100000:
            parser.error("input exceeds100000 events")
        rulefile = args.rules or args.input.with_name("rules.json")
        if args.rules and rulefile.exists():
            rules = MarketRules.from_payload(json.loads(rulefile.read_text(encoding="utf-8")))
        elif events and events[0].get("type") == "rules":
            rules = MarketRules.from_payload(events[0]["rules"])
        elif rulefile.exists():
            rules = MarketRules.from_payload(json.loads(rulefile.read_text(encoding="utf-8")))
        elif events and events[0].get("source") == "synthetic_demo":
            rules = MarketRules.fixture()
        else:
            parser.error("Public replay requires recorded exchange rules; synthetic fallback forbidden")
        initial = args.input.with_suffix(".initial.json")
        if initial.exists():
            seed = json.loads(initial.read_text(encoding="utf-8"))
    with runtime_lock(args.runtime):
        if args.command == "demo":
            result = run_replay(cfg, args.runtime, demo_events(args.steps), source="synthetic_demo", record=True)
        elif args.command == "replay":
            result = run_replay(cfg, args.runtime, events, rules, seed=seed)
        elif args.command == "compare":
            result = comparison(cfg, args.runtime, events, rules)
        else:
            result = asyncio.run(run_public(cfg, args.runtime, args.seconds, reset=reset))
            if reset and result.get("halt_reason") is not None:
                parser.error("reset refused: " + str(result["halt_reason"]) + "; review report/audit, account stays halted")
            if reset and result.get("halt_reason") is None:
                (args.runtime / "KILL").unlink(missing_ok=True)
                LOG.warning("Explicit reset passed; one next start approved, account and peak retained")
        # Full trade/audit detail is exported locally, keeping terminal output readable.
        print(json.dumps({key: value for key, value in result.items()
                          if key not in ("trades", "audit", "equity_curve", "rules")}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
