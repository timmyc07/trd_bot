"""Offline V2 watchdog integration; fake transport never touches a network."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from trd_bot.data import INFO_URL, MarketRules
from trd_bot.research_config import ResearchConfig
from trd_bot.research_runner import run_public


class FakeTransport:
    closed = False
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        self.closed = True


class FeedTests(unittest.TestCase):
    def test_watchdog_stops_frozen_feed_and_closes(self):
        root = Path(__file__).parents[1] / "runtime/tests"
        root.mkdir(parents=True, exist_ok=True)
        transport = FakeTransport()
        async def rules(*args):
            payload = MarketRules.fixture().to_payload()
            payload.update(source=INFO_URL, fetched_ms=int(time.time()*1000))
            return MarketRules.from_payload(payload)
        async def frozen(transport, emit):
            now = int(time.time()*1000)
            emit({"type":"book","id":"1","received_ms":now,"timestamp":now,
                "source":"binance_public","connection":"connected",
                "bids":[["60000","0.00003"]],"asks":[["60001","0.00001"]]})
            await asyncio.Event().wait()
        with tempfile.TemporaryDirectory(dir=root) as directory:
            with patch("trd_bot.research_runner.fetch_rules", rules), patch("trd_bot.research_runner.public_events", frozen):
                report = asyncio.run(run_public(replace(ResearchConfig(), max_data_age_ms=50),
                    Path(directory), 2, transport_factory=lambda:transport))
            self.assertEqual(report["halt_reason"],"stale_market_data")
            self.assertEqual(report["open_paper_orders"],0)
            self.assertTrue(transport.closed)
            self.assertIsNone(report["drawdown_safe"])


if __name__ == "__main__":
    unittest.main()
