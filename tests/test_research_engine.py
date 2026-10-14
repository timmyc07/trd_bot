from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import time
import unittest

from trd_bot.data import MarketRules
from trd_bot.research_config import ResearchConfig
from trd_bot.research_engine import ResearchEngine
from trd_bot.research_runner import demo_events, run_replay
from trd_bot import risk

EPOCH = MarketRules.fixture().fetched_ms


def book(n=0, bid="60000", ask="60001", bull=True):
    return {"type":"book","id":f"book-{n}","received_ms":EPOCH+n,"timestamp":EPOCH+n,
            "source":"synthetic_demo","connection":"connected",
            "bids":[[bid,"0.00003" if bull else "0.00001"]],
            "asks":[[ask,"0.00001" if bull else "0.00003"]]}


def trade(n=200, amount="0.00030", side="sell", price="59980"):
    return {"type":"trade","id":f"trade-{n}","received_ms":EPOCH+n,"timestamp":EPOCH+n,
            "source":"synthetic_demo","connection":"connected","side":side,"price":price,"amount":amount}


class ResearchTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).parents[1]/"runtime/tests"
        root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.path=Path(self.temp.name)
        self.cfg=ResearchConfig()
        self.engine=ResearchEngine(self.cfg,self.path,"synthetic_demo",MarketRules.fixture())

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def test_initial_zero_btc_cannot_sell(self):
        self.engine.process(book(bull=False))
        self.assertIsNone(self.engine.state["order"])
        self.assertEqual(self.engine.state["cash"],"50")
        self.assertEqual(risk.approve(self.engine.state,"sell",D("60001"),D("0.0001"),D("60000"),self.cfg),"insufficient_base")

    def test_quantized_budget_reserved_and_partial_fee_once(self):
        self.engine.process(book())
        before=self.engine.state
        self.assertLessEqual(D(before["reserved_cash"]),D("10"))
        self.assertEqual(before["cash"],"50")
        self.engine.process(trade())
        state=self.engine.state
        self.assertEqual(D(state["base"]),D("0.00003"))
        self.assertEqual(D(state["cash"]),D("48.19820000"))
        self.assertEqual(D(state["fees"]),D("0.0018"))
        self.assertEqual(D(self.engine.report()["equity"]),D("49.9982"))
        self.assertLess(D(state["reserved_cash"]),D(before["reserved_cash"]))
        self.engine.process(trade())
        self.assertEqual(self.engine.state,state)

    def test_partial_then_cancel_and_late_fill_is_ignored(self):
        self.engine.process(book())
        self.engine.process(trade())
        cancel={**trade(250),"type":"cancel","id":"cancel-1"}
        self.engine.process(cancel)
        before=self.engine.state
        self.assertEqual(before["reserved_cash"],"0")
        self.assertIsNone(before["order"])
        self.engine.process(trade(300))
        self.assertEqual(self.engine.state["cash"],before["cash"])
        self.assertEqual(self.engine.state["base"],before["base"])

    def test_cancel_before_trade_and_ttl(self):
        self.engine.process(book())
        self.engine.process({**trade(150),"type":"cancel","id":"first-cancel"})
        self.engine.process(trade(200))
        self.assertEqual(self.engine.report()["fills"],0)
        self.engine.process(book(250))
        for n in range(1250,30251,1000):
            self.engine.process(book(n,bull=False))
        self.assertIsNone(self.engine.state["order"])
        self.assertEqual(self.engine.state["reserved_cash"],"0")
        self.assertTrue(any(row["kind"]=="expired" for row in self.engine.ledger.audit()))

    def test_touch_does_not_fill_and_latency(self):
        self.engine.process(book())
        self.engine.process(trade(50))
        self.engine.process(trade(200,price="60000"))
        self.assertEqual(self.engine.report()["fills"],0)

    def test_projected_exposure_includes_pending_and_fees(self):
        self.engine.process(book())
        state=self.engine.state
        state.update(cash="31",base="0.00030",order=None,reserved_cash="0",reserved_base="0")
        self.assertEqual(risk.approve(state,"buy",D("60000"),D("0.00016"),D("60000"),self.cfg),"projected_exposure")

    def test_unrealized_drawdown_halts_and_preserves_peak(self):
        self.engine.process(book())
        for n in range(200,1200,200):
            self.engine.process(trade(n))
        self.engine.process(book(1500,bid="10000",ask="10001"))
        self.assertEqual(self.engine.state["halt_reason"],"drawdown")
        self.assertEqual(self.engine.state["reserved_cash"],"0")
        self.assertIsNone(self.engine.report()["drawdown_safe"])
        self.engine.close()
        self.engine=ResearchEngine(self.cfg,self.path,"synthetic_demo",MarketRules.fixture())
        self.assertEqual(self.engine.state["halt_reason"],"drawdown")
        self.assertEqual(self.engine.state["peak"],"50")

    def test_bad_missing_stale_and_out_of_order_data(self):
        self.engine.process(book())
        self.engine.process(trade(200))
        self.engine.process(trade(150))
        self.assertEqual(self.engine.state["halt_reason"],"data_or_rules_error")
        self.assertEqual(self.engine.report()["fills"],1)
        self.assertIsNone(self.engine.state["order"])
        for index,change in enumerate(({"timestamp":None},{"bids":[[]]},{"timestamp":EPOCH-6000})):
            other=ResearchEngine(self.cfg,self.path/f"bad{index}","synthetic_demo",MarketRules.fixture())
            other.process({**book(),**change})
            self.assertEqual(other.state["halt_reason"],"data_or_rules_error")
            other.close()

    def test_expired_rules_reject_new_order(self):
        payload=MarketRules.fixture().to_payload()
        payload["max_age_ms"]=1
        other=ResearchEngine(self.cfg,self.path/"expired","synthetic_demo",MarketRules.from_payload(payload))
        other.process(book(200))
        self.assertIsNone(other.state["order"])
        self.assertTrue(other.state["rejections"])
        other.close()

    def test_no_trade_baseline_and_deterministic_report(self):
        events=list(demo_events(30))
        first=run_replay(self.cfg,self.path/"one",events)
        second=run_replay(self.cfg,self.path/"two",events)
        self.assertEqual(first,second)
        self.assertEqual((self.path/"one/report.json").read_bytes(),(self.path/"two/report.json").read_bytes())
        baseline=run_replay(replace(self.cfg,strategy="no_trade"),self.path/"cash",events)
        self.assertEqual(D(baseline["equity"]),D("50"))
        self.assertEqual(baseline["fills"],0)

    def test_configuration_conflicts(self):
        for change in ({"fee_bps":D("10000")},{"order_budget":D("21")},{"initial_base":D(".1")},
                       {"max_open_orders":2},{"max_position_fraction":D("0")},{"duration_seconds":0}):
            with self.assertRaises(ValueError):
                replace(self.cfg,**change).validate()

    def test_balance_journal_disagreement_cannot_restart_or_refund(self):
        self.engine.ledger.update(lambda state: state.update(cash="51"), "corrupt-account-test")
        self.engine.close()
        with self.assertRaises(ValueError):
            ResearchEngine(self.cfg,self.path,"synthetic_demo",MarketRules.fixture())
        from trd_bot.ledger import read_snapshot
        saved=read_snapshot(self.path)
        self.assertEqual(saved["halt_reason"],"ledger_inconsistent")
        self.assertEqual(saved["cash"],"51")  # Evidence retained, never replaced with fresh funding.

    def test_live_execution_rechecks_age_after_persistence_delay(self):
        cfg=replace(self.cfg,max_data_age_ms=50)
        other=ResearchEngine(cfg,self.path/"live-age","binance_public")
        now=int(time.time()*1000)-200
        event={**book(),"source":"binance_public","timestamp":now,"received_ms":now}
        other.process(event)
        self.assertEqual(other.state["halt_reason"],"data_or_rules_error")
        self.assertEqual(other.state["cash"],"50")
        self.assertIsNone(other.state["order"])
        other.close()
