"""Legacy V1 offline regression engine and shared Decimal/JSON helpers.

The supported CLI and panel execute research_engine.py (V2), never this engine.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
import logging
import os
from pathlib import Path
import time

LOG = logging.getLogger("trd_bot")
ZERO = Decimal("0")
BPS = Decimal("10000")
WINDOWS = os.name == "nt"


def dec(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite financial value")
    return result


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, default=str, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    # Windows readers briefly deny rename; never fall back to a partial overwrite.
    for attempt in range(6 if WINDOWS else 1):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if not WINDOWS or attempt == 5:
                LOG.exception("Atomic state publication failed: %s", path)
                raise
            if attempt == 0:
                LOG.warning("Windows file busy; retrying atomic publication: %s", path)
            time.sleep(.01 * 2 ** attempt)


@dataclass(frozen=True)
class Config:
    symbol: str = "BTC/USDT"
    initial_cash: Decimal = Decimal("10000")
    initial_base: Decimal = Decimal("0.005")
    order_size: Decimal = Decimal("0.001")
    max_base: Decimal = Decimal("0.01")
    max_notional: Decimal = Decimal("1500")
    max_daily_loss: Decimal = Decimal("50")
    max_drawdown: Decimal = Decimal("100")
    half_spread_bps: Decimal = Decimal("4")
    skew_bps_per_unit: Decimal = Decimal("2")
    fee_bps: Decimal = Decimal("10")
    tick_size: Decimal = Decimal("0.01")
    latency_ms: int = 50
    max_data_age_ms: int = 2000
    max_spread_bps: Decimal = Decimal("50")
    refresh_ms: int = 1000
    max_orders_per_second: int = 4

    @classmethod
    def load(cls, path: Path):
        raw = json.loads(path.read_text(encoding="utf-8"))
        defaults = cls()
        for key, value in raw.items():
            if key not in {f.name for f in fields(cls)}:
                raise ValueError(f"Unknown configuration: {key}")
            if isinstance(getattr(defaults, key), Decimal):
                raw[key] = dec(value)
        cfg = cls(**raw)
        cfg.validate()
        return cfg

    def validate(self):
        for key, value in asdict(self).items():
            if isinstance(value, Decimal) and (not value.is_finite() or value < 0):
                raise ValueError(f"Invalid {key}")
        for key in ("order_size", "max_base", "max_notional", "max_daily_loss",
                    "max_drawdown", "tick_size", "half_spread_bps", "max_spread_bps"):
            if getattr(self, key) <= 0:
                raise ValueError(f"{key} must be positive")
        for key in ("latency_ms", "max_data_age_ms", "refresh_ms", "max_orders_per_second"):
            value = getattr(self, key)
            if type(value) is not int or value < (0 if key == "latency_ms" else 1):
                raise ValueError(f"Invalid {key}")
        if self.initial_base > self.max_base or self.order_size > self.max_base:
            raise ValueError("Initial base/order size exceeds inventory limit")
        if self.symbol != "BTC/USDT":
            raise ValueError("This version validates Binance BTC/USDT only")


@dataclass
class Order:
    side: str
    price: Decimal
    remaining: Decimal
    active_at: int
    source_after: int
    queue_ahead: Decimal


class Engine:
    def __init__(self, cfg: Config, directory: Path, source: str):
        cfg.validate()
        self.cfg, self.directory, self.source = cfg, directory, source
        directory.mkdir(parents=True, exist_ok=True)
        self.orders: list[Order] = []
        self.book = None
        self.last_book_ms = None
        self.last_event_ms = -1
        self.last_quote_ms = -cfg.refresh_ms
        self.order_times = deque()
        self.seen = set()
        self.seen_queue = deque()
        self.latencies = deque(maxlen=10000)
        self.cash, self.base = cfg.initial_cash, cfg.initial_base
        self.fees = ZERO
        self.fills = 0
        self.start_equity = self.day_equity = self.high_water = None
        self.day = None
        self.mark = None
        self.halt_reason = None
        self.errors = 0
        state = directory / "state.json"
        if state.exists():
            raw = json.loads(state.read_text(encoding="utf-8"))
            if raw["schema"] != 1:
                raise ValueError("Unsupported state schema")
            if raw["config"] != json.loads(json.dumps(asdict(cfg), default=str)):
                raise ValueError("Config changed: use a new runtime directory")
            if raw["source"] != source:
                raise ValueError("Data source changed: use a new runtime directory")
            for key in ("cash", "base", "fees", "start_equity", "day_equity", "high_water", "mark"):
                setattr(self, key, dec(raw[key]) if raw[key] is not None else None)
            for key in ("fills", "day", "halt_reason", "errors"):
                setattr(self, key, raw[key])
            if self.cash < 0 or not ZERO <= self.base <= cfg.max_base:
                raise ValueError("Corrupt account state")
            # Every restart requires review; never implicitly resume after a crash.
            self.halt_reason = self.halt_reason or "restart_requires_review"
            LOG.warning("Restored account; halt=%s", self.halt_reason)

    @property
    def equity(self):
        return self.cash + self.base * (self.mark or ZERO)

    def halt(self, reason: str):
        if not self.halt_reason:
            self.halt_reason = reason
            LOG.error("Risk halt: %s", reason)
        self.orders.clear()
        self.save()

    def guard(self, now: int, kill: bool = False):
        if kill:
            self.halt("kill_switch")
        if self.last_book_ms is not None and now - self.last_book_ms > self.cfg.max_data_age_ms:
            self.halt("stale_market_data")
        if self.start_equity is not None:
            day = datetime.fromtimestamp(now / 1000, timezone.utc).date().isoformat()
            # Check old day's loss before rolling the daily baseline.
            if self.day_equity - self.equity >= self.cfg.max_daily_loss:
                self.halt("daily_loss")
            self.high_water = max(self.high_water, self.equity)
            if self.high_water - self.equity >= self.cfg.max_drawdown:
                self.halt("drawdown")
            if self.base * self.mark > self.cfg.max_notional:
                self.halt("exposure")
            if day != self.day:
                self.day, self.day_equity = day, self.equity
        return not self.halt_reason

    def process(self, event: dict):
        start = time.perf_counter_ns()
        now = event["received_ms"]
        if type(now) is not int or now < 0 or now < self.last_event_ms:
            raise ValueError("Event receive time must be nondecreasing integer milliseconds")
        self.last_event_ms = now
        if not self.guard(now):
            return
        self.orders = [o for o in self.orders if o.side != "buy" or
                       (self.base + o.remaining) * self.mark <= self.cfg.max_notional]
        if event["type"] == "book":
            self.on_book(event, now)
        elif event["type"] == "trade":
            self.on_trade(event, now)
        else:
            raise ValueError("Unknown event type")
        self.guard(now)
        self.latencies.append((time.perf_counter_ns() - start) / 1000)
        self.save()

    def on_book(self, event, now):
        source_ms = event.get("timestamp")
        if type(source_ms) is not int or source_ms > now + 1000 or now - source_ms > self.cfg.max_data_age_ms:
            self.halt("invalid_book_timestamp")
            return
        bids, asks = [], []
        for name, target in (("bids", bids), ("asks", asks)):
            for level in event[name]:
                price, amount = dec(level[0]), dec(level[1])
                if price <= 0 or amount <= 0:
                    raise ValueError("Book levels must be positive")
                target.append((price, amount))
        if not bids or not asks or bids != sorted(bids, reverse=True) or asks != sorted(asks):
            raise ValueError("Empty or unsorted book")
        bid, bsize = bids[0]
        ask, asize = asks[0]
        if bid >= ask:
            raise ValueError("Crossed order book")
        self.mark = (bid + ask) / 2
        self.book = bids, asks
        self.last_book_ms = now
        if self.start_equity is None:
            self.start_equity = self.day_equity = self.high_water = self.equity
            self.day = datetime.fromtimestamp(now / 1000, timezone.utc).date().isoformat()
        if not self.guard(now):
            return
        self.orders = [o for o in self.orders if o.side != "buy" or
                       (self.base + o.remaining) * self.mark <= self.cfg.max_notional]
        if (ask - bid) / self.mark * BPS > self.cfg.max_spread_bps:
            self.halt("abnormal_spread")
            return
        if now - self.last_quote_ms < self.cfg.refresh_ms:
            return
        while self.order_times and now - self.order_times[0] >= 1000:
            self.order_times.popleft()
        if len(self.order_times) + 2 > self.cfg.max_orders_per_second:
            return
        self.orders.clear()  # Instant paper cancellation; limitation documented.
        fair = (bid * asize + ask * bsize) / (bsize + asize)
        inventory_units = (self.base - self.cfg.initial_base) / self.cfg.order_size
        center = fair * (1 - inventory_units * self.cfg.skew_bps_per_unit / BPS)
        half = fair * self.cfg.half_spread_bps / BPS
        for side, price, rounding, levels in (
            ("buy", min(center - half, bid), ROUND_FLOOR, bids),
            ("sell", max(center + half, ask), ROUND_CEILING, asks),
        ):
            price = (price / self.cfg.tick_size).to_integral_value(rounding=rounding) * self.cfg.tick_size
            size = self.cfg.order_size
            if price <= 0:
                continue
            if side == "buy" and (self.base + size > self.cfg.max_base or
                    (self.base + size) * self.mark > self.cfg.max_notional or
                    price * size * (1 + self.cfg.fee_bps / BPS) > self.cash):
                continue
            if side == "sell" and size > self.base:
                continue
            # Visible levels better than or at our price are ahead. Hidden liquidity unknown.
            queue = sum((qty for px, qty in levels if
                         (px >= price if side == "buy" else px <= price)), ZERO)
            self.orders.append(Order(side, price, size, now + self.cfg.latency_ms, now, queue))
            self.order_times.append(now)
        self.last_quote_ms = now

    def on_trade(self, event, now):
        timestamp = event.get("timestamp")
        if type(timestamp) is not int or timestamp > now + 1000 or now - timestamp > self.cfg.max_data_age_ms:
            raise ValueError("Missing/stale trade timestamp")
        trade_id = str(event["id"])
        if not trade_id or trade_id == "None":
            raise ValueError("Trade ID required")
        if trade_id in self.seen:
            return
        self.seen.add(trade_id)
        self.seen_queue.append(trade_id)
        if len(self.seen_queue) > 100000:
            self.seen.remove(self.seen_queue.popleft())
        price, volume = dec(event["price"]), dec(event["amount"])
        if price <= 0 or volume <= 0 or event["side"] not in ("buy", "sell"):
            raise ValueError("Invalid trade")
        for order in list(self.orders):
            if now < order.active_at or timestamp <= order.source_after + self.cfg.latency_ms or order.side == event["side"]:
                continue
            if (order.side == "buy" and price > order.price) or (order.side == "sell" and price < order.price):
                continue
            consumed = min(order.queue_ahead, volume)
            order.queue_ahead -= consumed
            volume -= consumed
            size = min(order.remaining, volume)
            if size <= 0:
                continue
            cost = size * order.price
            fee = cost * self.cfg.fee_bps / BPS
            if order.side == "buy" and (self.base + size > self.cfg.max_base or
                    (self.base + size) * self.mark > self.cfg.max_notional or cost + fee > self.cash):
                self.orders.remove(order)
                LOG.warning("Cancelled paper bid: projected exposure/budget exceeded")
                continue
            if order.side == "sell" and size > self.base:
                self.orders.remove(order)
                continue
            self.cash += (-cost if order.side == "buy" else cost) - fee
            self.base += size if order.side == "buy" else -size
            self.fees += fee
            self.fills += 1
            order.remaining -= size
            volume -= size
            LOG.info("Paper fill side=%s price=%s amount=%s fee=%s", order.side, order.price, size, fee)
            # State is authoritative; fills journal is diagnostic (not a transactional ledger).
            self.save()
            with (self.directory / "fills.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"received_ms": now, "side": order.side,
                    "price": str(order.price), "amount": str(size), "fee": str(fee)}) + "\n")
            if order.remaining == 0:
                self.orders.remove(order)
            if not self.guard(now):
                break

    def report(self):
        samples = sorted(self.latencies)
        return {"source": self.source, "mode": "paper_only", "symbol": self.cfg.symbol,
            "cash": str(self.cash), "base": str(self.base), "mark": str(self.mark),
            "equity": str(self.equity),
            "pnl": str(self.equity - self.start_equity) if self.start_equity is not None else None,
            "daily_pnl": str(self.equity - self.day_equity) if self.day_equity is not None else None,
            "drawdown": str(self.high_water - self.equity) if self.high_water is not None else None,
            "fees": str(self.fees), "fills": self.fills, "open_paper_orders": len(self.orders),
            "halt_reason": self.halt_reason, "errors": self.errors,
            "last_book_ms": self.last_book_ms,
            "decision_latency_us": {"samples": len(samples),
                "p50": samples[len(samples) // 2] if samples else None,
                "p99": samples[min(len(samples) - 1, int(len(samples) * .99))] if samples else None},
            "fill_model": "visible-queue approximation, partial fills, delayed activation; no market impact"}

    def save(self):
        state = {key: getattr(self, key) for key in ("cash", "base", "fees", "fills", "start_equity",
            "day_equity", "high_water", "day", "mark", "halt_reason", "errors")}
        state.update(config=asdict(self.cfg), source=self.source, schema=1)
        atomic_json(self.directory / "state.json", state)

    def health(self):
        value = self.report()
        value["heartbeat_utc"] = datetime.now(timezone.utc).isoformat()
        atomic_json(self.directory / "health.json", value)
