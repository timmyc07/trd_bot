from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from trd_bot.ledger import Ledger, read_snapshot


def initial():
    return {"cash": "50", "base": "0", "reserved_cash": "0", "reserved_base": "0",
            "fees": "0", "peak": "50", "fee_bps": "10", "order": None,
            "halt_reason": None}


def reserve_buy(state):
    state["order"] = {"side": "buy", "price": "100", "quantity": "0.09",
                      "remaining": "0.09", "status": "open"}
    state["reserved_cash"] = "9.009"


class LedgerTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parents[1] / "runtime" / "tests"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.directory = Path(self.temp.name)
        self.config = {"initial_cash": "50", "fee_bps": "10"}
        self.ledger = Ledger(self.directory, initial(), self.config, "synthetic_v1")

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def reopen(self):
        self.ledger.close()
        self.ledger = Ledger(self.directory, initial(), self.config, "synthetic_v1")

    def test_duplicate_does_not_execute_and_survives_reopen(self):
        self.assertTrue(self.ledger.apply("book:1", reserve_buy))
        self.reopen()
        self.assertFalse(self.ledger.apply("book:1", lambda _: self.fail("duplicate mutation")))
        self.assertEqual(self.ledger.event_count(), 1)
        self.assertEqual(self.ledger.state["reserved_cash"], "9.009")

    def test_exception_rolls_back_state_event_and_journals(self):
        def failed(state):
            state["cash"] = "1"
            raise RuntimeError("simulated failure before commit")
        with self.assertRaisesRegex(RuntimeError, "simulated failure"):
            self.ledger.apply("event:1", failed)
        self.assertEqual(self.ledger.state, initial())
        self.assertEqual(self.ledger.event_count(), 0)
        self.reopen()
        self.assertEqual(self.ledger.state, initial())
        self.assertTrue(self.ledger.apply("event:1", lambda _: None))

    def test_process_crash_rolls_back_uncommitted_event_and_funding_change(self):
        script = """import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('BEGIN IMMEDIATE')
connection.execute("INSERT INTO events VALUES('crashed:1')")
connection.execute("UPDATE account_state SET payload='{}'")
os._exit(23)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.ledger.path)],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 23)
        self.reopen()
        self.assertEqual(self.ledger.state, initial())
        self.assertEqual(self.ledger.event_count(), 0)

    def test_failure_after_state_and_fill_insert_rolls_back_every_row(self):
        def invalid_journal(state):
            state["cash"] = "49"
            return {"fills": [{"price": "100", "amount": "0.01"}], "audit": [object()]}
        with self.assertRaises(ValueError):
            self.ledger.apply("transaction:1", invalid_journal)
        self.reopen()
        self.assertEqual(self.ledger.state, initial())
        self.assertEqual(self.ledger.fills(), [])
        self.assertEqual(self.ledger.audit(), [])
        self.assertEqual(self.ledger.event_count(), 0)

    def test_partial_fill_then_cancel_releases_only_unfilled_reservation(self):
        self.ledger.apply("place:1", reserve_buy)
        def partial(state):
            state.update(cash="45.996", base="0.04", fees="0.004", reserved_cash="5.005")
            state["order"].update(remaining="0.05", status="partially_filled")
            return {"fills": [{"side": "buy", "price": "100", "amount": "0.04", "fee": "0.004"}]}
        self.ledger.apply("trade:1", partial)
        self.assertEqual(self.ledger.state["reserved_cash"], "5.005")
        def cancel(state):
            state.update(order=None, reserved_cash="0")
            return {"audit": [{"type": "cancel", "remaining": "0.05"}]}
        self.ledger.apply("cancel:1", cancel)
        self.reopen()
        self.assertEqual(self.ledger.state["cash"], "45.996")
        self.assertEqual(self.ledger.state["base"], "0.04")
        self.assertEqual(self.ledger.state["fees"], "0.004")
        self.assertEqual(len(self.ledger.fills()), 1)
        self.assertEqual(self.ledger.audit(), [{"type": "cancel", "remaining": "0.05"}])

    def test_sell_reserves_existing_base_and_partial_releases(self):
        state = initial()
        state.update(cash="40", base="0.1", reserved_base="0.04", order={
            "side": "sell", "price": "100", "quantity": "0.04", "remaining": "0.04", "status": "new"})
        Ledger.reconcile(state)
        state.update(base="0.08", reserved_base="0.02", cash="41.998", fees="0.002")
        state["order"].update(remaining="0.02", status="partially_filled")
        Ledger.reconcile(state)
        state["reserved_base"] = "0.04"
        with self.assertRaises(ValueError):
            Ledger.reconcile(state)

    def test_bad_reservations_and_order_boundaries_rejected(self):
        cases = []
        for key, value in (("cash", "-1"), ("base", "NaN"), ("fees", "Infinity"),
                           ("peak", "-0.01"), ("cash", 50), ("reserved_cash", "1")):
            state = initial()
            state[key] = value
            cases.append(state)
        buy = initial()
        reserve_buy(buy)
        for key, value in (("remaining", "0"), ("remaining", "0.1"), ("price", "0"),
                           ("status", "filled"), ("reserved_cash", "9")):
            state = deepcopy(buy)
            state["order"][key] = value
            cases.append(state)
        buy["cash"] = "9"
        cases.append(buy)
        sell = initial()
        sell.update(reserved_base="1", order={"side": "sell", "price": "100",
                    "quantity": "1", "remaining": "1", "status": "open"})
        cases.append(sell)
        for state in cases:
            with self.subTest(state=state), self.assertRaises(ValueError):
                Ledger.reconcile(state)

    def test_reconcile_rejection_is_atomic(self):
        def invalid(state):
            reserve_buy(state)
            state["reserved_cash"] = "9"
        with self.assertRaises(ValueError):
            self.ledger.apply("place:bad", invalid)
        self.assertEqual(self.ledger.state, initial())
        self.assertEqual(self.ledger.event_count(), 0)

    def test_external_state_property_cannot_mutate_account(self):
        self.ledger.apply("place:1", reserve_buy)
        state = self.ledger.state
        state["cash"] = "999"
        state["order"]["remaining"] = "100"
        self.assertEqual(self.ledger.state["cash"], "50")
        self.assertEqual(self.ledger.state["order"]["remaining"], "0.09")

    def test_mutator_retained_reference_cannot_change_committed_state(self):
        retained = []
        def keep_reference(state):
            retained.append(state)
        self.ledger.apply("event:retain", keep_reference)
        retained[0]["cash"] = "999"
        self.assertEqual(self.ledger.state["cash"], "50")

    def test_config_and_source_mismatch_refused(self):
        self.ledger.close()
        for config, source in (({"initial_cash": "100", "fee_bps": "10"}, "synthetic_v1"),
                               (self.config, "binance_public")):
            with self.subTest(config=config, source=source), self.assertRaises(ValueError):
                Ledger(self.directory, initial(), config, source)

    def test_missing_database_never_reinitializes_funds(self):
        self.ledger.close()
        self.ledger.path.unlink()
        with self.assertRaisesRegex(ValueError, "Missing ledger"):
            Ledger(self.directory, initial(), self.config, "synthetic_v1")
        self.assertFalse(self.ledger.path.exists())

    def test_corrupt_database_refused(self):
        self.ledger.close()
        self.ledger.path.write_bytes(b"not a sqlite database")
        with self.assertRaises(ValueError):
            Ledger(self.directory, initial(), self.config, "synthetic_v1")
        self.assertEqual(self.ledger.path.read_bytes(), b"not a sqlite database")

    def test_structurally_valid_database_with_invalid_account_refused(self):
        self.ledger.close()
        state = initial()
        state["cash"] = "-1"
        connection = sqlite3.connect(self.ledger.path)
        try:
            connection.execute("UPDATE account_state SET payload=?", (json.dumps(state),))
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(ValueError, "cash"):
            Ledger(self.directory, initial(), self.config, "synthetic_v1")

    def test_legacy_account_not_migrated_or_reset(self):
        legacy = self.directory / "legacy"
        legacy.mkdir()
        (legacy / "state.json").write_text('{"cash":"123"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Legacy"):
            Ledger(legacy, initial(), self.config, "synthetic_v1")
        self.assertFalse((legacy / "ledger.sqlite3").exists())

    def test_halt_and_control_audit_preserved_without_implicit_resume(self):
        def halt(state):
            state["halt_reason"] = "drawdown"
            return {"audit": [{"action": "halt", "reason": "drawdown"}]}
        self.ledger.update(halt, event_id="halt:1")
        self.reopen()
        self.assertEqual(self.ledger.state["halt_reason"], "drawdown")
        self.assertEqual(self.ledger.state["peak"], "50")
        self.assertFalse(self.ledger.update(halt, event_id="halt:1"))
        self.assertEqual(len(self.ledger.audit()), 1)

    def test_financial_values_stay_text_and_exact(self):
        def precise(state):
            state["cash"] = "49.99999999999999999999999999"
            return {"audit": [{"cash": state["cash"]}]}
        self.ledger.apply("precise:1", precise)
        self.reopen()
        self.assertEqual(Decimal(self.ledger.state["cash"]), Decimal("49.99999999999999999999999999"))
        storage = self.ledger.connection.execute("SELECT typeof(payload) FROM account_state").fetchone()[0]
        self.assertEqual(storage, "text")

    def test_missing_marker_refused(self):
        self.ledger.close()
        self.ledger.marker.unlink()
        with self.assertRaisesRegex(ValueError, "Missing ledger"):
            Ledger(self.directory, initial(), self.config, "synthetic_v1")

    def test_read_snapshot_does_not_mutate_state_or_append_control(self):
        self.ledger.apply("place:read", reserve_buy)
        before = self.ledger.path.read_bytes()
        before_count = self.ledger.event_count()
        state = read_snapshot(self.directory)
        self.assertEqual(state, self.ledger.state)
        self.assertEqual(self.ledger.event_count(), before_count)
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertEqual(state["halt_reason"], None)

    def test_read_snapshot_missing_database_does_not_create_directory_or_funds(self):
        missing = self.directory / "never-created"
        with self.assertRaisesRegex(ValueError, "Missing ledger"):
            read_snapshot(missing)
        self.assertFalse(missing.exists())

    def test_read_snapshot_corrupt_reservations_refused(self):
        self.ledger.connection.execute("UPDATE account_state SET payload=?",
            (json.dumps({**initial(), "reserved_cash": "1"}),))
        with self.assertRaisesRegex(ValueError, "reservations"):
            read_snapshot(self.directory)

    def session_seed(self):
        original = Ledger(self.directory / "session-original", initial(), self.config, "binance_public")
        try:
            original.apply("binance_public:place:1", reserve_buy)
            def partial(state):
                state.update(cash="45.996", base="0.04", fees="0.004", reserved_cash="5.005")
                state["order"].update(remaining="0.05", status="partially_filled")
                return {"fills": [{"id": "binance_public:trade:1", "side": "buy", "price": "100",
                                   "amount": "0.04", "fee": "0.004"}]}
            original.apply("binance_public:trade:1", partial)
            def cancel(state):
                state.update(order=None, reserved_cash="0", origin="binance_public", reset_approved=False)
                return {"audit": [{"event_id": "control:cancel:1", "kind": "cancelled", "reason": "manual"}]}
            original.apply("control:cancel:1", cancel)
            count, before = original.event_count(), original.path.read_bytes()
            snapshot = original.snapshot()
            self.assertEqual(original.event_count(), count)
            self.assertEqual(original.path.read_bytes(), before)
            self.assertEqual(snapshot["state"], original.state)
            self.assertEqual(snapshot["fills"], original.fills())
            self.assertEqual(snapshot["audit"], original.audit())
            return snapshot
        finally:
            original.close()

    def test_session_seed_preserves_balances_history_and_duplicate_ids(self):
        seed = self.session_seed()
        replay = Ledger(self.directory / "seeded-replay", initial(), self.config, "recorded_replay", seed=seed)
        try:
            self.assertEqual(replay.state, seed["state"])
            self.assertEqual(replay.fills(), seed["fills"])
            self.assertEqual(replay.audit(), seed["audit"])
            self.assertEqual(replay.event_count(), len(seed["event_ids"]))
            self.assertFalse(replay.apply("binance_public:trade:1", lambda _: self.fail("old fill replayed twice")))
            self.assertEqual(replay.snapshot()["origin"], "binance_public")
            replay.close()
            replay = Ledger(self.directory / "seeded-replay", initial(), self.config, "recorded_replay")
            self.assertEqual(replay.state["cash"], "45.996")
            self.assertEqual(replay.state["base"], "0.04")
        finally:
            replay.close()

    def test_session_seed_cannot_replace_existing_funds(self):
        seed = self.session_seed()
        with self.assertRaisesRegex(ValueError, "new ledger"):
            Ledger(self.directory, initial(), self.config, "synthetic_v1", seed=seed)
        self.assertEqual(self.ledger.state, initial())

    def test_bad_session_seed_refused_before_database_creation(self):
        valid = self.session_seed()
        cases = []
        for field, value in (("origin", "unknown"), ("config", {"initial_cash": "100"}),
                             ("event_ids", ["duplicate", "duplicate"]),
                             ("fills", [{"id": "unknown"}]), ("audit", [{"event_id": "unknown"}])):
            seed = deepcopy(valid)
            seed[field] = value
            cases.append(seed)
        for field, value in (("halt_reason", "drawdown"), ("cash", "-1"), ("origin", "synthetic_demo")):
            seed = deepcopy(valid)
            seed["state"][field] = value
            cases.append(seed)
        pending = deepcopy(valid)
        reserve_buy(pending["state"])
        cases.append(pending)
        for index, seed in enumerate(cases):
            target = self.directory / f"bad-seed-{index}"
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                Ledger(target, initial(), self.config, "recorded_replay", seed=seed)
            self.assertFalse((target / "ledger.sqlite3").exists())
            self.assertFalse((target / ".ledger-initialized").exists())

    def test_session_seed_never_funds_live_account(self):
        seed = self.session_seed()
        target = self.directory / "not-live"
        with self.assertRaisesRegex(ValueError, "only for replay"):
            Ledger(target, initial(), self.config, "binance_public", seed=seed)
        self.assertFalse((target / "ledger.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
