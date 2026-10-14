"""Trade-evidence-only conservative partial fill model, with no external orders."""
from decimal import Decimal, ROUND_FLOOR

from .core import dec


def eligible_fill(order, trade, cfg, step_size):
    now, timestamp = trade["received_ms"], trade["timestamp"]
    if now < order["active_at"] or timestamp <= order["active_at"] or trade["side"] == order["side"]:
        return Decimal(0)
    price, limit, amount = dec(trade["price"]), dec(order["price"]), dec(trade["amount"])
    penetration = cfg.slippage_bps / 10000
    if ((order["side"] == "buy" and price > limit * (1 - penetration)) or
            (order["side"] == "sell" and price < limit * (1 + penetration))):
        return Decimal(0)
    queue = dec(order["queue_ahead"])
    consumed = min(queue, amount)
    order["queue_ahead"] = str(queue - consumed)
    # Queue depletion uses observed volume once; our fill has a separate participation cap.
    quantity = min(dec(order["remaining"]), amount - consumed, amount * cfg.participation)
    return (quantity / step_size).to_integral_value(rounding=ROUND_FLOOR) * step_size
