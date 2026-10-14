"""Versioned, validated research settings; all budgets include quote-currency fees."""
from dataclasses import asdict, dataclass, fields
from decimal import Decimal
import json
from pathlib import Path

from .core import dec


@dataclass(frozen=True)
class ResearchConfig:
    schema: int = 2
    symbol: str = "BTCUSDT"
    initial_cash: Decimal = Decimal("50")
    initial_base: Decimal = Decimal("0")
    order_budget: Decimal = Decimal("10")
    max_position_fraction: Decimal = Decimal("0.40")
    drawdown_fraction: Decimal = Decimal("0.05")
    fee_bps: Decimal = Decimal("10")
    fee_source: str = "research assumption; quote-currency fee on each side"
    fee_date: str = "2026-10-14"
    strategy: str = "imbalance"
    imbalance_buy: Decimal = Decimal("0.60")
    imbalance_sell: Decimal = Decimal("0.40")
    latency_ms: int = 100
    slippage_bps: Decimal = Decimal("1")
    participation: Decimal = Decimal("0.10")
    queue_multiplier: Decimal = Decimal("1")
    max_open_orders: int = 1
    order_ttl_ms: int = 30000
    max_data_age_ms: int = 5000
    rules_max_age_ms: int = 3600000
    duration_seconds: int = 60
    recording_max_bytes: int = 10000000
    retention_max_bytes: int = 100000000
    queue_size: int = 2048

    @classmethod
    def load(cls, path: Path):
        raw = json.loads(path.read_text(encoding="utf-8"))
        defaults = cls()
        if not isinstance(raw, dict) or set(raw) - {f.name for f in fields(cls)}:
            raise ValueError("Unknown research configuration; legacy config requires separate runtime")
        for key, value in raw.items():
            if isinstance(getattr(defaults, key), Decimal):
                raw[key] = dec(value)
        result = cls(**raw)
        result.validate()
        return result

    def validate(self):
        if self.schema != 2 or self.symbol != "BTCUSDT" or self.max_open_orders != 1:
            raise ValueError("Only schema2 BTCUSDT with one unfinished paper order is supported")
        if self.strategy not in ("imbalance", "no_trade"):
            raise ValueError("Unsupported fixed baseline strategy")
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, Decimal) and (not value.is_finite() or value < 0):
                raise ValueError(f"Invalid {field.name}")
        for key in ("order_ttl_ms", "max_data_age_ms", "rules_max_age_ms", "duration_seconds",
                    "recording_max_bytes", "retention_max_bytes", "queue_size", "latency_ms"):
            value = getattr(self, key)
            if type(value) is not int or value < (0 if key == "latency_ms" else 1):
                raise ValueError(f"Invalid {key}")
        if self.initial_cash <= 0 or self.initial_base != 0:
            raise ValueError("Research account starts with positive cash and zero BTC")
        if self.fee_bps >= 10000:
            raise ValueError("Fee assumption must be less than100%")
        if not 0 < self.max_position_fraction < 1 or not 0 < self.drawdown_fraction < 1:
            raise ValueError("Exposure/drawdown fractions must be between0 and1")
        if not 0 < self.participation <= 1 or self.queue_multiplier < 1:
            raise ValueError("Invalid conservative execution model")
        if not 0 < self.imbalance_sell < self.imbalance_buy < 1:
            raise ValueError("Strategy thresholds conflict")
        if self.order_budget <= 0 or self.order_budget > self.initial_cash * self.max_position_fraction:
            raise ValueError("Order budget conflicts with initial equity exposure limit")
        if self.retention_max_bytes < self.recording_max_bytes or not self.fee_source or not self.fee_date:
            raise ValueError("Recording limits or fee provenance missing")

    def serializable(self):
        return json.loads(json.dumps(asdict(self), default=str))
