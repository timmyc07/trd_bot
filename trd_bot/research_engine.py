"""Ordered V2 engine. Each event commits dedupe, account, order and journal together."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import logging
from pathlib import Path
import threading
import time

from .core import dec
from .data import INFO_URL, MarketRules
from .ledger import Ledger
from .paper_execution import eligible_fill
from . import risk, strategy

LOG = logging.getLogger("trd_bot")
ZERO = Decimal(0)


class HaltSignal:
    """Watchdog can stop new decisions without waiting for the persistence worker."""
    def __init__(self):
        self.reason = None
        self.lock = threading.Lock()

    def request(self, reason):
        with self.lock:
            self.reason = self.reason or reason


class ResearchEngine:
    def __init__(self, cfg, directory, source, rules=None, signal=None, seed=None):
        cfg.validate()
        if source not in ("synthetic_demo", "binance_public", "recorded_replay"):
            raise ValueError("Unsupported data source")
        self.cfg, self.directory, self.source = cfg, Path(directory), source
        self.signal = signal or HaltSignal()
        initial = {"cash": str(cfg.initial_cash), "base": str(cfg.initial_base),
            "reserved_cash": "0", "reserved_base": "0", "fees": "0", "fee_bps": str(cfg.fee_bps),
            "peak": str(cfg.initial_cash), "initial_equity": str(cfg.initial_cash),
            "order": None, "order_counter": 0, "halt_reason": None, "bid": None, "ask": None,
            "last_received_ms": None, "last_book_ms": None, "last_source_ms": {}, "events": 0,
            "rules": rules.to_payload() if rules else None, "book": None, "data_fresh": False,
            "fill_count": 0, "errors": 0, "rejections": {}, "coverage_start_ms": None, "coverage_end_ms": None,
            "max_drawdown": "0", "passive_breach": False, "connection": "connecting"}
        initial.update(session_start_ms=None, session_end_ms=None, session_events=0)
        existing = (self.directory / "ledger.sqlite3").exists()
        self.ledger = Ledger(self.directory, initial, cfg.serializable(), source, seed=seed)
        try:
            self.reconcile_history()
        except (ValueError, KeyError, ArithmeticError):
            self.control("ledger_inconsistent")
            self.close()
            raise
        if existing:
            self.control("restart_requires_review")

    def reconcile_history(self):
        """Rebuild assets from funding and committed fills, without SQL floating point."""
        cash, base, fees = self.cfg.initial_cash, self.cfg.initial_base, ZERO
        fills = self.ledger.fills()
        for fill in fills:
            price, quantity, fee = (dec(fill[key]) for key in ("price", "amount", "fee"))
            if price <= 0 or quantity <= 0 or fee != price * quantity * self.cfg.fee_bps / 10000:
                raise ValueError("Fill journal disagrees with configured fees")
            if fill["side"] not in ("buy", "sell"):
                raise ValueError("Invalid fill side")
            cash += (-price * quantity if fill["side"] == "buy" else price * quantity) - fee
            base += quantity if fill["side"] == "buy" else -quantity
            fees += fee
            if cash < 0 or base < 0:
                raise ValueError("Committed fill history has negative assets")
        state = self.state
        if any(dec(state[key]) != value for key, value in (("cash", cash), ("base", base), ("fees", fees))):
            raise ValueError("Account balance disagrees with committed fill journal")
        if state["fill_count"] != len(fills):
            raise ValueError("Fill counter disagrees with journal")
        if state["order"]:
            order = state["order"]
            filled = sum((dec(fill["amount"]) for fill in fills if fill["order_id"] == order["id"]), ZERO)
            if dec(order["quantity"]) - filled != dec(order["remaining"]):
                raise ValueError("Pending remaining quantity disagrees with committed fills")

    @property
    def state(self):
        return self.ledger.state

    def _row(self, state, kind, reason, event_id, now):
        row = {"kind": kind, "reason": reason, "event_id": event_id, "received_ms": now}
        if kind in ("rejected", "halt", "passive_exposure"):
            LOG.warning("Research %s reason=%s event=%s", kind, reason, event_id)
        return row

    def _cancel(self, state, rows, reason, event_id, now):
        order = state["order"]
        if order:
            kind = "expired" if reason == "ttl" else "completed" if reason == "completed" else "cancelled"
            row = self._row(state, kind, reason, event_id, now)
            row.update(order_id=order["id"], remaining=order["remaining"])
            rows.append(row)
        state["order"] = None
        state["reserved_cash"] = state["reserved_base"] = "0"

    def _halt(self, state, rows, reason, event_id, now):
        if reason != "restart_requires_review":
            state["reset_approved"] = False
        if not state["halt_reason"]:
            rows.append(self._row(state, "halt", reason, event_id, now))
            state["halt_reason"] = reason
        state["data_fresh"] = False
        state["connection"] = "halted"
        self._cancel(state, rows, reason, event_id, now)

    def _reject(self, state, rows, reason, event_id, now):
        state["rejections"][reason] = state["rejections"].get(reason, 0) + 1
        rows.append(self._row(state, "rejected", reason, event_id, now))

    def control(self, reason, now=None):
        state = self.state
        now = now if now is not None else state["last_received_ms"] or 0
        event_id = f"control:{self.ledger.event_count()}:{reason}"
        def mutate(current):
            rows = []
            self._halt(current, rows, reason, event_id, now)
            return {"audit": rows}
        self.ledger.apply(event_id, mutate)

    def reset(self, rules, book, wall_ms=lambda: int(time.time()*1000)):
        """One-use manual unlock; never reset cash, peak or dedupe history."""
        now = book["received_ms"]
        self._validate_book(book, now)
        if book["source"] != self.source or self.source != "binance_public":
            raise ValueError("Only the original public account can be manually unlocked")
        if rules.source != INFO_URL:
            raise ValueError("Public reset requires public rules")
        # Validate rule freshness independent of minimum affordable order size.
        if not 0 <= now - rules.fetched_ms <= min(rules.max_age_ms, self.cfg.rules_max_age_ms):
            raise ValueError("Reset requires fresh exchange rules")
        event_id = f"control:{self.ledger.event_count()}:manual_reset"
        def mutate(state):
            rows = []
            self.reconcile_history()
            current_ms = wall_ms()
            if self.signal.reason or not 0 <= current_ms - now <= self.cfg.max_data_age_ms:
                raise ValueError("Reset data expired or watchdog stopped the connection")
            self._validate_book(book, current_ms)
            bid = dec(book["bids"][0][0])
            # Exercise all applicable filters, including freshness of average-price references.
            price, quantity = rules.quantize(bid, self.cfg.order_budget / bid, "buy")
            rules.validate(price, quantity, "buy", current_ms)
            value = risk.equity(state, bid)
            if value <= 0 or (dec(state["peak"]) - value) / dec(state["peak"]) >= self.cfg.drawdown_fraction:
                raise ValueError("Drawdown still exceeds limit; reset refused")
            self._cancel(state, rows, "reset_reconcile", event_id, now)
            state.update(halt_reason=None, reset_approved=True, rules=rules.to_payload(),
                         bid=str(bid), ask=str(book["asks"][0][0]), data_fresh=False, connection="reset_checked")
            rows.append(self._row(state, "manual_reset", "local_human_acknowledgment", event_id, now))
            return {"audit": rows}
        self.ledger.apply(event_id, mutate)

    def consume_reset(self):
        if not self.state.get("reset_approved"):
            return False
        event_id = f"control:{self.ledger.event_count()}:consume_reset"
        def mutate(state):
            if state["halt_reason"] not in (None, "restart_requires_review"):
                return {}
            state.update(halt_reason=None, reset_approved=False, data_fresh=False, connection="connecting",
                         session_start_ms=None, session_end_ms=None, session_events=0)
            return {"audit": [self._row(state, "reset_consumed", "explicit_local_reset", event_id, state["last_received_ms"] or 0)]}
        self.ledger.apply(event_id, mutate)
        return not self.state["halt_reason"]

    def _validate_book(self, event, now):
        self._validate_time(event, now)
        for key, reverse in (("bids", True), ("asks", False)):
            levels = event[key]
            if not isinstance(levels, list) or not levels:
                raise ValueError("Incomplete book")
            parsed = [(dec(level[0]), dec(level[1])) for level in levels]
            if any(p <= 0 or q <= 0 for p, q in parsed) or parsed != sorted(parsed, reverse=reverse):
                raise ValueError("Invalid book levels")
            if len({p for p, _ in parsed}) != len(parsed):
                raise ValueError("Duplicate price levels")
        if dec(event["bids"][0][0]) >= dec(event["asks"][0][0]):
            raise ValueError("Crossed book")

    def _validate_time(self, event, now):
        if self.source == "binance_public" and int(time.time()*1000) - now > self.cfg.max_data_age_ms:
            raise ValueError("Queued data expired before execution")
        timestamp = event.get("timestamp")
        if type(timestamp) is not int or timestamp < 0 or timestamp > now + 1000 or now - timestamp > self.cfg.max_data_age_ms:
            raise ValueError("Missing, future or stale source timestamp")
        if event.get("connection") != "connected":
            raise ValueError("Market data is disconnected")

    def process(self, event):
        if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not event["id"]:
            self.control("invalid_event_id")
            return False
        event_id = f"{event.get('source')}:{self.cfg.symbol}:{event.get('type')}:{event['id']}"
        now = event.get("received_ms")
        if type(now) is not int or now < 0:
            self.control("invalid_receive_time")
            return False
        def mutate(state):
            rows, fills = [], []
            before = deepcopy(state)
            try:
                if event.get("type") == "control":
                    if state["last_received_ms"] is not None and now < state["last_received_ms"]:
                        raise ValueError("Out-of-order control time")
                    state["last_received_ms"] = now
                    state["coverage_end_ms"] = now
                    state["events"] += 1
                    state["session_end_ms"] = now
                    state["session_events"] = state.get("session_events", 0) + 1
                    self._halt(state, rows, str(event["reason"]), event_id, now)
                    return {"audit": rows}
                if self.signal.reason:
                    self._halt(state, rows, self.signal.reason, event_id, now)
                if state["halt_reason"]:
                    return {"audit": rows}
                if event.get("source") not in ("synthetic_demo", "binance_public"):
                    raise ValueError("Unknown provenance")
                expected = self.source if self.source != "recorded_replay" else state.get("origin")
                if expected and event["source"] != expected:
                    raise ValueError("Mixed market data sources")
                state["origin"] = event["source"]
                if state["last_received_ms"] is not None and now < state["last_received_ms"]:
                    raise ValueError("Out-of-order receive time")
                kind = event.get("type")
                state["last_received_ms"] = now
                state["coverage_start_ms"] = state["coverage_start_ms"] if state["coverage_start_ms"] is not None else now
                state["coverage_end_ms"] = now
                state["events"] += 1
                state["session_start_ms"] = state.get("session_start_ms") if state.get("session_start_ms") is not None else now
                state["session_end_ms"] = now
                state["session_events"] = state.get("session_events", 0) + 1
                if kind == "control":
                    self._halt(state, rows, str(event["reason"]), event_id, now)
                    return {"audit": rows}
                if kind == "rules":
                    rules = MarketRules.from_payload(event["rules"])
                    if event["source"] == "binance_public" and rules.source != INFO_URL:
                        raise ValueError("Public data cannot use synthetic exchange rules")
                    state["rules"] = rules.to_payload()
                    return {}
                self._validate_time(event, now)
                previous = state["last_source_ms"].get(kind)
                if previous is not None and event["timestamp"] < previous:
                    raise ValueError("Out-of-order source time")
                state["last_source_ms"][kind] = event["timestamp"]
                if kind == "book":
                    self._validate_book(event, now)
                    state.update(bid=str(event["bids"][0][0]), ask=str(event["asks"][0][0]),
                        last_book_ms=now, book=deepcopy(event), data_fresh=True, connection="connected")
                elif kind == "trade":
                    if event["side"] not in ("buy", "sell") or dec(event["price"]) <= 0 or dec(event["amount"]) <= 0:
                        raise ValueError("Invalid trade")
                elif kind == "cancel":
                    self._cancel(state, rows, "explicit_cancel", event_id, now)
                    return {"audit": rows}
                else:
                    raise ValueError("Unknown event type")
                if state["last_book_ms"] is None:
                    return {}
                if now - state["last_book_ms"] > self.cfg.max_data_age_ms:
                    self._halt(state, rows, "stale_market_data", event_id, now)
                    return {"audit": rows}
                order = state["order"]
                if order and now >= order["expires_at"]:
                    self._cancel(state, rows, "ttl", event_id, now)
                bid = dec(state["bid"])
                value = risk.equity(state, bid)
                state["peak"] = str(max(dec(state["peak"]), value))
                drawdown = (dec(state["peak"]) - value) / dec(state["peak"])
                state["max_drawdown"] = str(max(dec(state["max_drawdown"]), drawdown))
                if drawdown >= self.cfg.drawdown_fraction:
                    self._halt(state, rows, "drawdown", event_id, now)
                    return {"audit": rows}
                position = dec(state["base"]) * bid / value if value > 0 else Decimal(1)
                breach = position > self.cfg.max_position_fraction
                if breach and not state["passive_breach"]:
                    rows.append(self._row(state, "passive_exposure", "price_move_blocks_buys", event_id, now))
                state["passive_breach"] = breach
                order = state["order"]
                if order and order["side"] == "buy":
                    simulated = {**state, "order": None, "reserved_cash": "0", "reserved_base": "0"}
                    why = risk.approve(simulated, "buy", dec(order["price"]), dec(order["remaining"]), bid, self.cfg)
                    if why:
                        self._cancel(state, rows, why, event_id, now)
                if kind == "trade" and state["order"]:
                    self._fill(state, event, event_id, rows, fills)
                if kind == "book" and state["order"] is None and not self.signal.reason:
                    intent = strategy.propose(state, event, self.cfg)
                    if intent:
                        self._submit(state, intent, event_id, now, rows)
                if self.signal.reason:
                    self._halt(state, rows, self.signal.reason, event_id, now)
                value = risk.equity(state, bid)
                state["peak"] = str(max(dec(state["peak"]), value))
                dd = (dec(state["peak"]) - value) / dec(state["peak"])
                state["max_drawdown"] = str(max(dec(state["max_drawdown"]), dd))
                if dd >= self.cfg.drawdown_fraction:
                    self._halt(state, rows, "drawdown", event_id, now)
                rows.append({"kind": "equity", "event_id": event_id, "received_ms": now,
                    "cash": state["cash"], "base": state["base"], "equity": str(value),
                    "peak": state["peak"], "drawdown": str(dd),
                    "position_fraction": str(dec(state["base"]) * bid / value if value > 0 else 1)})
            except (ValueError, KeyError, TypeError, IndexError, ArithmeticError) as exc:
                state.clear()
                state.update(before)
                rows.clear()
                fills.clear()
                state["errors"] += 1
                self._halt(state, rows, "data_or_rules_error", event_id, now)
                rows.append(self._row(state, "invalid_data", str(exc), event_id, now))
            return {"fills": fills, "audit": rows}
        return self.ledger.apply(event_id, mutate)

    def _submit(self, state, intent, event_id, now, rows):
        try:
            if not state["rules"]:
                raise ValueError("Unknown market rules")
            rules = MarketRules.from_payload(state["rules"])
            if now - rules.fetched_ms > self.cfg.rules_max_age_ms:
                raise ValueError("Expired market rules")
            price, quantity = rules.quantize(intent["price"], intent["quantity"], intent["side"])
            rules.validate(price, quantity, intent["side"], now)
        except (ValueError, ArithmeticError) as exc:
            self._reject(state, rows, str(exc), event_id, now)
            return
        why = risk.approve(state, intent["side"], price, quantity, dec(state["bid"]), self.cfg)
        if why or (intent["side"] == "buy" and state["passive_breach"]):
            self._reject(state, rows, why or "passive_exposure", event_id, now)
            return
        state["order_counter"] += 1
        levels = state["book"]["bids" if intent["side"] == "buy" else "asks"]
        queue = sum((dec(q) for p, q in levels if
                    (dec(p) >= price if intent["side"] == "buy" else dec(p) <= price)), ZERO)
        state["order"] = {"id": f"order-{state['order_counter']}", "side": intent["side"],
            "price": str(price), "quantity": str(quantity), "remaining": str(quantity),
            "status": "new", "active_at": now + self.cfg.latency_ms,
            "expires_at": now + self.cfg.order_ttl_ms, "queue_ahead": str(queue * self.cfg.queue_multiplier)}
        if intent["side"] == "buy":
            state["reserved_cash"] = str(price * quantity * (1 + self.cfg.fee_bps / 10000))
        else:
            state["reserved_base"] = str(quantity)
        rows.append({**self._row(state, "new", "risk_approved", event_id, now), "order_id": state["order"]["id"]})

    def _fill(self, state, event, event_id, rows, fills):
        if self.signal.reason:
            return
        order = state["order"]
        rules = MarketRules.from_payload(state["rules"])
        quantity = eligible_fill(order, event, self.cfg, rules.step_size)
        if quantity <= 0:
            return
        price = dec(order["price"])
        simulated = {**state, "order": None, "reserved_cash": "0", "reserved_base": "0"}
        why = risk.approve(simulated, order["side"], price, quantity, dec(state["bid"]), self.cfg)
        if why:
            self._cancel(state, rows, why, event_id, event["received_ms"])
            return
        cost, fee = price * quantity, price * quantity * self.cfg.fee_bps / 10000
        state["cash"] = str(dec(state["cash"]) + (-cost if order["side"] == "buy" else cost) - fee)
        state["base"] = str(dec(state["base"]) + (quantity if order["side"] == "buy" else -quantity))
        state["fees"] = str(dec(state["fees"]) + fee)
        state["fill_count"] += 1
        remaining = dec(order["remaining"]) - quantity
        order["remaining"] = str(remaining)
        order["status"] = "partially_filled" if remaining else "filled"
        fills.append({"id": event_id, "order_id": order["id"], "received_ms": event["received_ms"],
            "side": order["side"], "price": str(price), "amount": str(quantity), "fee": str(fee), "status": order["status"]})
        LOG.info("Research paper fill side=%s amount=%s price=%s fee=%s", order["side"], quantity, price, fee)
        if remaining:
            state["reserved_cash"] = str(remaining * price * (1 + self.cfg.fee_bps / 10000)) if order["side"] == "buy" else "0"
            state["reserved_base"] = str(remaining) if order["side"] == "sell" else "0"
        else:
            self._cancel(state, rows, "completed", event_id, event["received_ms"])

    def report(self):
        state = self.state
        bid = dec(state["bid"]) if state["bid"] is not None else ZERO
        value = risk.equity(state, bid)
        dd = (dec(state["peak"]) - value) / dec(state["peak"])
        fresh = state["data_fresh"] and not state["halt_reason"]
        return {"source": self.source, "origin": state.get("origin", self.source), "mode": "paper_only",
            "schema": 2, "engine_version": "0.2.0", "symbol": "BTC/USDT", "config": self.cfg.serializable(),
            "cash": state["cash"], "base": state["base"], "reserved_cash": state["reserved_cash"],
            "reserved_base": state["reserved_base"], "available_cash": str(dec(state["cash"])-dec(state["reserved_cash"])),
            "available_base": str(dec(state["base"])-dec(state["reserved_base"])),
            "mark": state["bid"], "mark_method": "last valid best bid; stale values are historical only",
            "equity": str(value), "pnl": str(value-dec(state["initial_equity"])),
            "fees": state["fees"], "fills": state["fill_count"], "peak": state["peak"],
            "drawdown": str(max(ZERO, dec(state["peak"])-value)), "drawdown_fraction": str(dd),
            "max_drawdown_fraction": state["max_drawdown"], "position_fraction": str(dec(state["base"])*bid/value if value>0 else 1),
            "data_fresh": fresh, "drawdown_safe": dd < self.cfg.drawdown_fraction if fresh else None,
            "open_paper_orders": int(state["order"] is not None), "order": state["order"],
            "halt_reason": state["halt_reason"], "errors": state["errors"],
            "last_book_ms": state["last_book_ms"], "events": state["events"], "rejections": state["rejections"],
            "rules": state["rules"], "fee_provenance": {"date": self.cfg.fee_date, "source": self.cfg.fee_source},
            "fill_model": {"latency_ms": self.cfg.latency_ms, "slippage_penetration_bps": str(self.cfg.slippage_bps),
                "participation": str(self.cfg.participation), "queue_multiplier": str(self.cfg.queue_multiplier),
                "limit_execution_price": True, "market_impact": False},
            "decision_latency_us": {"samples": state["events"], "p50": None, "p99": None}}

    def close(self):
        self.ledger.close()
