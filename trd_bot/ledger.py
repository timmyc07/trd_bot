"""One SQLite transaction owns state, event deduplication and its journal rows."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import logging
import os
from pathlib import Path
import sqlite3
import uuid

LOG = logging.getLogger("trd_bot")
ZERO = Decimal("0")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _amount(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a Decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid {field}") from exc
    if not amount.is_finite() or amount < ZERO:
        raise ValueError(f"Invalid {field}")
    return amount


class Ledger:
    """Use from one owner thread; callers serialize controls and market events."""

    def __init__(self, directory: Path, initial_state: dict, config: dict, source: str, *, seed=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "ledger.sqlite3"
        self.marker = self.directory / ".ledger-initialized"
        self.connection = None
        self._state = None
        if not isinstance(config, dict) or not isinstance(source, str) or not source:
            raise ValueError("Ledger requires configuration and source")
        config_json = _json(config)
        exists = self.path.exists()
        seeded_events, seeded_fills, seeded_audit = [], [], []
        if seed is not None:
            if exists:
                raise ValueError("Session seed requires a new ledger; existing funds cannot be replaced")
            initial_state, seeded_events, seeded_fills, seeded_audit = self._validate_seed(seed, config_json, source)
        if exists != self.marker.exists():
            raise ValueError("Missing ledger database or initialization marker; never reset funds implicitly")
        if (self.directory / "state.json").exists():
            raise ValueError("Legacy state.json must use its original engine and runtime")
        if not exists:
            self.reconcile(initial_state)
            initial_json = _json(initial_state)
            # An interrupted initialization must never look like a new funded account.
            with self.marker.open("x", encoding="utf-8") as handle:
                handle.write(_json({"schema": 1}))
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                descriptor = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        try:
            if json.loads(self.marker.read_text(encoding="utf-8")) != {"schema": 1}:
                raise ValueError("Invalid initialization marker")
            # mode=rw refuses to recreate a database deleted after the existence check.
            uri = self.path.resolve().as_uri() + ("?mode=rw" if exists else "?mode=rwc")
            self.connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA synchronous=FULL")
            if exists:
                if self.connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Ledger integrity check failed")
                metadata = dict(self.connection.execute("SELECT key,value FROM metadata"))
                if metadata != {"schema": "1", "config": config_json, "source": source}:
                    raise ValueError("Ledger configuration/source/schema mismatch")
                if self.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("Ledger journal references are inconsistent")
                rows = self.connection.execute("SELECT payload FROM account_state").fetchall()
                if len(rows) != 1:
                    raise ValueError("Ledger must contain exactly one account state")
                self._state = json.loads(rows[0][0])
                self.reconcile(self._state)
            else:
                self.connection.execute("BEGIN IMMEDIATE")
                for statement in (
                    "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
                    "CREATE TABLE account_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)",
                    "CREATE TABLE events(id TEXT PRIMARY KEY)",
                    "CREATE TABLE fills(seq INTEGER PRIMARY KEY,event_id TEXT NOT NULL REFERENCES events(id),payload TEXT NOT NULL)",
                    "CREATE TABLE audit(seq INTEGER PRIMARY KEY,event_id TEXT NOT NULL REFERENCES events(id),payload TEXT NOT NULL)",
                ):
                    self.connection.execute(statement)
                self.connection.executemany("INSERT INTO metadata VALUES(?,?)",
                    (("schema", "1"), ("config", config_json), ("source", source)))
                self.connection.execute("INSERT INTO account_state VALUES(1,?)", (initial_json,))
                self.connection.executemany("INSERT INTO events(id) VALUES(?)", ((value,) for value in seeded_events))
                for table, rows in (("fills", seeded_fills), ("audit", seeded_audit)):
                    self.connection.executemany(f"INSERT INTO {table}(event_id,payload) VALUES(?,?)", rows)
                self.connection.commit()
                self._state = deepcopy(initial_state)
            LOG.info("SQLite ledger opened source=%s path=%s", source, self.path)
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            self.close()
            LOG.error("Ledger initialization/load refused: %s", exc)
            raise ValueError(f"Ledger initialization/load refused: {exc}") from exc

    @staticmethod
    def _validate_seed(seed, config_json, source):
        required = {"state", "event_ids", "fills", "audit", "config", "origin"}
        if not isinstance(seed, dict) or set(seed) != required:
            raise ValueError("Invalid session seed fields")
        if source != "recorded_replay" or seed["origin"] not in ("synthetic_demo", "binance_public"):
            raise ValueError("Session seed is only for replay of a known original source")
        if _json(seed["config"]) != config_json:
            raise ValueError("Session seed configuration mismatch")
        state = deepcopy(seed["state"])
        Ledger.reconcile(state)
        if state.get("halt_reason", "missing") is not None or state["order"] is not None:
            raise ValueError("Session seed must be reconciled and manually unlocked without pending orders")
        if state.get("origin", seed["origin"]) != seed["origin"]:
            raise ValueError("Session seed mixes data origins")
        ids = seed["event_ids"]
        if not isinstance(ids, list) or any(not isinstance(value, str) or not value for value in ids):
            raise ValueError("Invalid session event IDs")
        known = set(ids)
        if len(known) != len(ids):
            raise ValueError("Duplicate session event IDs")
        journals = []
        for table, key in (("fills", "id"), ("audit", "event_id")):
            rows = seed[table]
            if not isinstance(rows, list):
                raise ValueError(f"Invalid session {table}")
            journal = []
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get(key), str) or row[key] not in known:
                    raise ValueError(f"Session {table} references an unknown event")
                journal.append((row[key], _json(row)))
            journals.append(journal)
        return state, list(ids), journals[0], journals[1]

    @property
    def state(self):
        return deepcopy(self._state)

    @staticmethod
    def reconcile(state):
        if not isinstance(state, dict):
            raise ValueError("Ledger state must be an object")
        amounts = {field: _amount(state.get(field), field) for field in
                   ("cash", "base", "reserved_cash", "reserved_base", "fees", "peak", "fee_bps")}
        if amounts["cash"] < amounts["reserved_cash"] or amounts["base"] < amounts["reserved_base"]:
            raise ValueError("Reservations exceed total balances")
        expected_cash = expected_base = ZERO
        if "order" not in state:
            raise ValueError("Missing order state")
        order = state["order"]
        if order is not None:
            if not isinstance(order, dict) or order.get("side") not in ("buy", "sell"):
                raise ValueError("Invalid single pending order")
            price, quantity, remaining = (_amount(order.get(key), f"order.{key}")
                                          for key in ("price", "quantity", "remaining"))
            if price <= ZERO or quantity <= ZERO or not ZERO < remaining <= quantity:
                raise ValueError("Invalid order price/quantity/remaining")
            status = order.get("status")
            if not isinstance(status, str) or not status or status in {
                "filled", "completed", "cancelled", "canceled", "expired", "rejected"
            }:
                raise ValueError("Terminal orders must release reservations and clear pending order")
            if order["side"] == "buy":
                expected_cash = remaining * price * (1 + amounts["fee_bps"] / Decimal("10000"))
            else:
                expected_base = remaining
            for key, expected in (("reserved_cash", expected_cash), ("reserved_base", expected_base)):
                if key in order and _amount(order[key], f"order.{key}") != expected:
                    raise ValueError(f"Order {key} disagrees with remaining quantity")
        if amounts["reserved_cash"] != expected_cash or amounts["reserved_base"] != expected_base:
            raise ValueError("Account reservations disagree with pending order")
        for key, expected in (("available_cash", amounts["cash"] - expected_cash),
                              ("available_base", amounts["base"] - expected_base)):
            if key in state and _amount(state[key], key) != expected:
                raise ValueError(f"Invalid {key}")

    def apply(self, event_id: str, mutator) -> bool:
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("A nonempty event ID is required")
        if self.connection is None:
            raise ValueError("Ledger is closed")
        candidate = deepcopy(self._state)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            inserted = self.connection.execute("INSERT OR IGNORE INTO events(id) VALUES(?)", (event_id,))
            if not inserted.rowcount:
                self.connection.rollback()
                return False
            # Reload inside the write transaction; never overwrite a newer committed state.
            candidate = json.loads(self.connection.execute(
                "SELECT payload FROM account_state WHERE singleton=1").fetchone()[0])
            self.reconcile(candidate)
            result = mutator(candidate)
            if result is None:
                result = {}
            if not isinstance(result, dict) or set(result) - {"fills", "audit"}:
                raise ValueError("Mutator must return optional fills/audit lists")
            self.reconcile(candidate)
            self.connection.execute("UPDATE account_state SET payload=? WHERE singleton=1", (_json(candidate),))
            for table in ("fills", "audit"):
                rows = result.get(table, [])
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise ValueError(f"{table} must be a list of objects")
                self.connection.executemany(f"INSERT INTO {table}(event_id,payload) VALUES(?,?)",
                    ((event_id, _json(row)) for row in rows))
            self.connection.commit()
            self._state = deepcopy(candidate)
            return True
        except Exception:
            if self.connection.in_transaction:
                self.connection.rollback()
            LOG.exception("Ledger transaction rolled back event=%s", event_id)
            raise

    def update(self, mutator, event_id: str | None = None) -> bool:
        return self.apply(event_id if event_id is not None else "control:" + uuid.uuid4().hex, mutator)

    def _journal(self, table):
        if self.connection is None:
            raise ValueError("Ledger is closed")
        return [json.loads(row[0]) for row in self.connection.execute(f"SELECT payload FROM {table} ORDER BY seq")]

    def fills(self):
        return self._journal("fills")

    def audit(self):
        return self._journal("audit")

    def event_count(self):
        if self.connection is None:
            raise ValueError("Ledger is closed")
        return self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def snapshot(self):
        """Capture the complete session starting point in one read transaction."""
        if self.connection is None:
            raise ValueError("Ledger is closed")
        try:
            self.connection.execute("BEGIN")
            metadata = dict(self.connection.execute("SELECT key,value FROM metadata"))
            state = json.loads(self.connection.execute(
                "SELECT payload FROM account_state WHERE singleton=1").fetchone()[0])
            self.reconcile(state)
            origin = metadata["source"]
            if origin == "recorded_replay":
                origin = state.get("origin", origin)
            result = {"state": state, "event_ids": [row[0] for row in
                self.connection.execute("SELECT id FROM events ORDER BY rowid")],
                "fills": self.fills(), "audit": self.audit(), "config": json.loads(metadata["config"]),
                "origin": origin}
            self.connection.commit()
            return result
        except Exception:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def read_snapshot(directory: Path) -> dict:
    """Read an existing account without creating files, controls or funding."""
    directory = Path(directory)
    path, marker = directory / "ledger.sqlite3", directory / ".ledger-initialized"
    if not path.is_file() or not marker.is_file():
        raise ValueError("Missing ledger database or initialization marker")
    connection = None
    try:
        if json.loads(marker.read_text(encoding="utf-8")) != {"schema": 1}:
            raise ValueError("Invalid initialization marker")
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                     isolation_level=None, timeout=2)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Ledger integrity check failed")
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        if (set(metadata) != {"schema", "config", "source"} or metadata["schema"] != "1"
                or not metadata["source"] or not isinstance(json.loads(metadata["config"]), dict)):
            raise ValueError("Invalid ledger metadata")
        rows = connection.execute("SELECT payload FROM account_state").fetchall()
        if len(rows) != 1:
            raise ValueError("Ledger must contain exactly one account state")
        state = json.loads(rows[0][0])
        Ledger.reconcile(state)
        return state
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        raise ValueError(f"Read-only ledger snapshot refused: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
