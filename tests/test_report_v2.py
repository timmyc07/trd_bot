import csv
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from trd_bot.report import compare_results, export_report, split_events


class ReportTests(unittest.TestCase):
    def test_deterministic_export_and_decimal_fee_csv(self):
        root = Path(__file__).parents[1] / "runtime" / "tests"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder:
            directory = Path(folder)
            summary = {"source": "synthetic_demo", "cash": Decimal("39.990"),
                       "base": Decimal("0.00010"), "fees": Decimal("0.010"),
                       "config": {"initial_cash": "50"}, "fills": 1}
            fills = [{"id": "fill,\"one\"", "order_id": "order-1", "received_ms": 123,
                      "side": "buy", "price": Decimal("100000"), "amount": Decimal("0.00010"),
                      "fee": Decimal("0.010"), "status": "partial"}]
            audit = [{"event_id": "event-1", "received_ms": 124, "kind": "rejected",
                      "reason": "rule, \"quantity\"\nminimum"},
                     {"event_id": "event-2", "received_ms": 125, "kind": "cancelled", "reason": "operator"}]
            equity = [{"event_id": "event-1", "received_ms": 123, "equity": Decimal("49.990")}]
            report = export_report(directory, summary, fills, audit, equity, {"events": 2})
            first = {path.name: path.read_bytes() for path in directory.iterdir()}
            export_report(directory, dict(reversed(list(summary.items()))), fills, audit, equity, {"events": 2})
            self.assertEqual(first, {path.name: path.read_bytes() for path in directory.iterdir()})
            self.assertEqual(report, json.loads((directory / "report.json").read_text(encoding="utf-8")))
            self.assertEqual(report["research_limits"]["data_label"], "合成資料／非歷史績效")
            self.assertFalse(report["research_limits"]["profitability_verified"])
            with (directory / "trades.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["id"], fills[0]["id"])
            self.assertEqual(rows[0]["fee"], "0.010")
            with (directory / "rejections.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["reason"], audit[0]["reason"])

    def test_chronological_split_has_no_shared_receive_time(self):
        events = [{"received_ms": stamp} for stamp in (1, 2, 3, 3, 3, 4, 5)]
        development, holdout = split_events(events, .5)
        self.assertEqual(development + holdout, events)
        self.assertEqual(len(development), 5)
        self.assertLess(development[-1]["received_ms"], holdout[0]["received_ms"])
        self.assertEqual(split_events([]), ([], []))

    def test_rejects_invalid_split_and_unordered_events(self):
        for fraction in (0, 1, -.2, float("nan"), True, "0.7"):
            with self.subTest(fraction=fraction), self.assertRaises(ValueError):
                split_events([], fraction)
        for stamps in ((2, 1), (-1,), (True,), (1.5,), (None,)):
            with self.subTest(stamps=stamps), self.assertRaises(ValueError):
                split_events([{"received_ms": stamp} for stamp in stamps])

    def test_comparison_requires_baseline_and_orders_scenarios(self):
        results = {"slow": {"pnl": "-1"}, "no_trade": {"pnl": "0"}, "fee_zero": {"pnl": "1"}}
        comparison = compare_results(results)
        self.assertEqual(list(comparison["scenarios"]), ["fee_zero", "slow"])
        self.assertEqual(comparison["no_trade"], {"pnl": "0"})
        with self.assertRaises(ValueError):
            compare_results({"slow": {}})

    def test_replayed_synthetic_data_remains_labelled(self):
        root = Path(__file__).parents[1] / "runtime" / "tests"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder:
            report = export_report(Path(folder), {"source": "recorded_replay"}, [], [], [],
                                   {"data_kind": "synthetic_demo"})
            self.assertTrue(report["research_limits"]["synthetic_data"])
