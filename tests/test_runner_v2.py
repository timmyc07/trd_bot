import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from trd_bot.data import INFO_URL, MarketRules
from trd_bot.ledger import read_snapshot
from trd_bot.research_config import ResearchConfig
from trd_bot.research_engine import ResearchEngine
from trd_bot.research_runner import Recorder, run_public, run_replay, watch_silence


class Transport:
    async def __aenter__(self):
        return self
    async def __aexit__(self,*args):
        pass


async def rules(*args):
    raw=MarketRules.fixture().to_payload()
    raw.update(fetched_ms=int(time.time()*1000),source=INFO_URL)
    return MarketRules.from_payload(raw)


async def frozen(transport,emit):
    now=int(time.time()*1000)
    emit({"type":"book","id":f"book:{now}","received_ms":now,"timestamp":now,
          "source":"binance_public","connection":"connected",
          "bids":[["60000",".00003"]],"asks":[["60001",".00001"]]})
    await asyncio.Event().wait()


class RunnerTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).parents[1]/"runtime/tests"
        root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.path=Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_queue_full_stops_instead_of_dropping_and_trading(self):
        async def flood(transport,emit):
            for index in range(10):
                now=int(time.time()*1000)
                emit({"type":"book","id":str(index),"received_ms":now,"timestamp":now,
                      "source":"binance_public","connection":"connected","bids":[["60000",".00003"]],"asks":[["60001",".00001"]]})
        with patch("trd_bot.research_runner.fetch_rules",rules),patch("trd_bot.research_runner.public_events",flood):
            result=asyncio.run(run_public(replace(ResearchConfig(),queue_size=1),self.path,1,Transport))
        self.assertEqual(result["halt_reason"],"event_queue_overflow")
        self.assertEqual(result["fills"],0)
        self.assertEqual(result["reserved_cash"],"0")

    def test_watchdog_runs_while_ledger_worker_waits(self):
        original=ResearchEngine.process
        observed=[]
        entered=threading.Event()
        async def armed_watchdog(cfg,signal,last_seen,stop):
            while not entered.is_set() and not stop.is_set():
                await asyncio.sleep(.005)
            last_seen[0]=time.monotonic()
            await watch_silence(cfg,signal,last_seen,stop)
        def slow(engine,event):
            if event["type"]=="book":
                entered.set()
                time.sleep(.2)
                observed.append(engine.signal.reason)
            return original(engine,event)
        with patch("trd_bot.research_runner.fetch_rules",rules),patch("trd_bot.research_runner.public_events",frozen),patch.object(ResearchEngine,"process",slow),patch("trd_bot.research_runner.watch_silence",armed_watchdog):
            result=asyncio.run(run_public(replace(ResearchConfig(),max_data_age_ms=70),self.path,1,Transport))
        self.assertIn("stale_market_data",observed)
        self.assertEqual(result["halt_reason"],"stale_market_data")
        self.assertEqual(result["fills"],0)

    def test_recording_capacity_stops_without_removing_old_data(self):
        cfg=replace(ResearchConfig(),recording_max_bytes=100,retention_max_bytes=200)
        self.path.mkdir(exist_ok=True)
        with Recorder(self.path,cfg) as recorder:
            with self.assertRaises(ValueError):
                recorder.write({"large":"x"*1000})
        self.assertEqual((self.path/"events.jsonl").stat().st_size,0)

    def test_two_public_sessions_have_independent_replay_seeds(self):
        cfg=replace(ResearchConfig(),max_data_age_ms=1000)
        with patch("trd_bot.research_runner.fetch_rules",rules),patch("trd_bot.research_runner.public_events",frozen):
            first=asyncio.run(run_public(cfg,self.path,.25,Transport))
            reset=asyncio.run(run_public(cfg,self.path,1,Transport,reset=True))
            self.assertIsNone(reset["halt_reason"])
            second=asyncio.run(run_public(cfg,self.path,.25,Transport))
        captures=sorted(self.path.glob("events-*.jsonl"),key=lambda p:int(p.stem.split('-')[-1]))
        self.assertEqual(len(captures),2)
        for index,(path,live) in enumerate(zip(captures,(first,second))):
            events=[json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            seed=json.loads(path.with_suffix(".initial.json").read_text(encoding="utf-8"))
            result=run_replay(cfg,self.path/f"replay{index}",events,MarketRules.from_payload(events[0]["rules"]),seed=seed)
            for key in ("cash","base","fees","fills","equity","peak","halt_reason"):
                self.assertEqual(result[key],live[key])
            self.assertEqual(result["trades"],live["trades"])

    def test_reset_watchdog_race_refuses_unlock(self):
        cfg=replace(ResearchConfig(),max_data_age_ms=100)
        with patch("trd_bot.research_runner.fetch_rules",rules),patch("trd_bot.research_runner.public_events",frozen):
            asyncio.run(run_public(cfg,self.path,.05,Transport))
            original=ResearchEngine.reset
            def delayed(engine,*args):
                time.sleep(.2)
                return original(engine,*args)
            with patch.object(ResearchEngine,"reset",delayed):
                result=asyncio.run(run_public(cfg,self.path,1,Transport,reset=True))
        self.assertIsNotNone(result["halt_reason"])
        state=read_snapshot(self.path)
        self.assertFalse(state.get("reset_approved",False))

    def test_duration_zero_forbidden(self):
        with self.assertRaises(ValueError):
            asyncio.run(run_public(ResearchConfig(),self.path,0,Transport))
