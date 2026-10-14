"""Offline async integration: watchdog and cleanup without exchange access."""
import asyncio
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import time
from types import ModuleType
import unittest
from unittest.mock import patch

from trd_bot.cli import paper
from trd_bot.core import Config, Engine


class FrozenExchange:
    has = {"watchOrderBook": True, "watchTrades": True}
    def __init__(self):
        self.sent = False
        self.closed = False
    async def load_markets(self):
        return {}
    async def watch_order_book(self, *args):
        if not self.sent:
            self.sent = True
            return {"timestamp": None, "bids": [[60000, .002]], "asks": [[60001, .002]]}
        if self.sent is True:
            self.sent = "timestamped"
            return {"timestamp": int(time.time()*1000), "bids": [[60000, .002]], "asks": [[60001, .002]]}
        await asyncio.Event().wait()
    async def watch_trades(self, *args):
        await asyncio.Event().wait()
    async def close(self):
        self.closed = True


class FeedTests(unittest.TestCase):
    def test_watchdog_stops_frozen_feed_and_closes(self):
        root = Path(__file__).parents[1] / "runtime" / "tests"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            engine = Engine(replace(Config(), max_data_age_ms=50), Path(directory), "binance_public")
            exchange = FrozenExchange()
            module = ModuleType("ccxt.pro")
            module.binance = lambda options: exchange
            package = ModuleType("ccxt")
            package.pro = module
            with patch.dict(sys.modules, {"ccxt": package, "ccxt.pro": module}):
                asyncio.run(paper(engine, 2))
            self.assertEqual(engine.halt_reason, "stale_market_data")
            self.assertEqual(engine.orders, [])
            self.assertTrue(exchange.closed)

