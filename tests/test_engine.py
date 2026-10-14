from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from trd_bot.core import Config, Engine
from trd_bot.cli import demo_events, replay, runtime_lock

EPOCH = 1767225600000


def book(now=EPOCH, bid="99", ask="101", size="0.001"):
    return {"type": "book", "received_ms": now, "timestamp": now,
            "bids": [[bid, size]], "asks": [[ask, size]]}


def trade(now=EPOCH + 100, amount="0.003", side="sell", price="98", id="1"):
    return {"type": "trade", "received_ms": now, "timestamp": now,
            "id": id, "amount": amount, "side": side, "price": price}


class EngineTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).parents[1] / "runtime" / "tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        self.path = Path(self.temp.name)
        self.cfg = replace(Config(), max_spread_bps=D("1000"))
        self.engine = Engine(self.cfg, self.path, "test")

    def tearDown(self):
        self.temp.cleanup()

    def test_rounding_and_no_crossing(self):
        engine = Engine(replace(self.cfg, tick_size=D("0.05")), self.path / "ticks", "test")
        engine.process(book(bid="99.03", ask="100.02"))
        bid, ask = engine.orders
        self.assertEqual(bid.price % D("0.05"), 0)
        self.assertEqual(ask.price % D("0.05"), 0)
        self.assertLessEqual(bid.price, D("99.03"))
        self.assertGreaterEqual(ask.price, D("100.02"))

    def test_queue_partial_fees_and_duplicate(self):
        self.engine.process(book())
        self.engine.process(trade(amount="0.0015"))
        self.assertEqual(self.engine.base, D("0.0055"))
        self.assertEqual(self.engine.fills, 1)
        self.assertEqual(self.engine.fees, D("0.0000495"))
        before = self.engine.cash
        self.engine.process(trade(amount="0.0015"))
        self.assertEqual(self.engine.cash, before)
        self.engine.process(trade(now=EPOCH+200, id="2", amount="0.003"))
        self.assertEqual(self.engine.base, D("0.006"))
        self.assertEqual(self.engine.fills, 2)

    def test_wrong_side_and_delayed_activation(self):
        self.engine.process(book())
        self.engine.process(trade(now=EPOCH+20))
        self.assertEqual(self.engine.fills, 0)

    def test_pending_bid_cancelled_when_projected_exposure_rises(self):
        engine = Engine(replace(self.cfg, max_notional=D("0.605")), self.path / "exposure", "test")
        engine.process(book())
        self.assertIn("buy", [o.side for o in engine.orders])
        engine.process(book(EPOCH+100, bid="101", ask="103"))
        self.assertNotIn("buy", [o.side for o in engine.orders])

    def test_old_book_does_not_enable_pre_activation_trade(self):
        event = book()
        event["timestamp"] = EPOCH-1000
        self.engine.process(event)
        late = trade()
        late["timestamp"] = EPOCH+20
        self.engine.process(late)
        self.assertEqual(self.engine.fills, 0)
        event = trade(now=EPOCH+100, id="2")
        event["timestamp"] = EPOCH+10
        self.engine.process(event)
        self.assertEqual(self.engine.fills, 0)
        self.engine.process(trade(now=EPOCH+200, side="buy", price="98", id="3"))
        self.assertEqual(self.engine.fills, 0)

    def test_projected_inventory_and_spot_budget(self):
        self.engine.base = D("0.0095")
        self.engine.process(book())
        self.assertEqual([o.side for o in self.engine.orders], ["sell"])
        empty = Engine(replace(self.cfg, initial_cash=D("0"), initial_base=D("0")), self.path / "empty", "test")
        empty.process(book())
        self.assertEqual(empty.orders, [])

    def test_kill_stale_and_sticky_restart(self):
        self.engine.process(book())
        self.engine.guard(EPOCH+3000)
        self.assertEqual(self.engine.halt_reason, "stale_market_data")
        self.assertFalse(self.engine.orders)
        restored = Engine(self.cfg, self.path, "test")
        self.assertEqual(restored.halt_reason, "stale_market_data")
        self.engine.guard(EPOCH+3100, True)
        self.assertEqual(self.engine.halt_reason, "stale_market_data")

    def test_kill_before_fill(self):
        (self.path / "KILL").touch()
        replay(self.engine, [book(), trade()])
        self.assertEqual(self.engine.fills, 0)
        self.assertEqual(self.engine.halt_reason, "kill_switch")

    def test_daily_loss_and_drawdown(self):
        for key, reason in (("max_daily_loss", "daily_loss"), ("max_drawdown", "drawdown")):
            cfg = replace(self.cfg, **{key: D("0.1")})
            engine = Engine(cfg, self.path / reason, "test")
            engine.process(book())
            engine.process(book(EPOCH+100, bid="49", ask="51"))
            self.assertEqual(engine.halt_reason, reason)
            self.assertEqual(engine.orders, [])

    def test_bad_books_and_ordering(self):
        event = book()
        event["timestamp"] = None
        self.engine.process(event)
        self.assertEqual(self.engine.halt_reason, "invalid_book_timestamp")
        for bid, ask in (("nan", "101"), ("102", "101"), ("0", "101")):
            engine = Engine(self.cfg, self.path / bid, "test")
            with self.assertRaises(ValueError):
                engine.process(book(bid=bid, ask=ask))
        engine = Engine(self.cfg, self.path / "time", "test")
        engine.process(book())
        with self.assertRaises(ValueError):
            engine.process(book(EPOCH-1))

    def test_config_and_corrupt_state_fail_closed(self):
        with self.assertRaises(ValueError):
            replace(self.cfg, tick_size=D("0")).validate()
        with self.assertRaises(ValueError):
            replace(self.cfg, max_data_age_ms=True).validate()
        self.engine.save()
        (self.path / "state.json").write_text("{", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            Engine(self.cfg, self.path, "test")

    def test_restart_requires_review_and_preserves_equity(self):
        self.engine.process(book())
        self.engine.process(trade())
        restored = Engine(self.cfg, self.path, "test")
        self.assertEqual(restored.cash, self.engine.cash)
        self.assertEqual(restored.base, self.engine.base)
        self.assertEqual(restored.start_equity, self.engine.start_equity)
        self.assertEqual(restored.halt_reason, "restart_requires_review")

    def test_demo_replay_deterministic(self):
        events = list(demo_events(100))
        first = Engine(Config(), self.path / "first", "synthetic_demo")
        second = Engine(Config(), self.path / "second", "recorded_replay")
        replay(first, events)
        replay(second, events)
        for key in ("cash", "base", "fills", "fees", "pnl", "halt_reason"):
            self.assertEqual(first.report()[key], second.report()[key])

    def test_rate_and_exclusive_runtime(self):
        cfg = replace(self.cfg, refresh_ms=1, max_orders_per_second=2)
        engine = Engine(cfg, self.path / "rate", "test")
        engine.process(book())
        original = engine.orders[0]
        engine.process(book(EPOCH+100))
        self.assertIs(engine.orders[0], original)
        with runtime_lock(self.path / "locked"):
            with self.assertRaises(FileExistsError):
                with runtime_lock(self.path / "locked"):
                    pass

    def test_no_private_exchange_api(self):
        source = (Path(__file__).parents[1] / "trd_bot" / "cli.py").read_text(encoding="utf-8")
        for forbidden in ("create_order", "cancel_all_orders", "fetch_balance", "apiKey", "secret"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
