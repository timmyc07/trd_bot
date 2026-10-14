"""Independent spot risk checks, including fee-adjusted post-fill equity."""
from .core import dec


def equity(state, bid):
    return dec(state["cash"]) + dec(state["base"]) * bid


def approve(state, side, price, quantity, bid, cfg):
    if state.get("halt_reason"):
        return "halted"
    if side not in ("buy", "sell") or price <= 0 or quantity <= 0:
        return "invalid_intent"
    fee = price * quantity * cfg.fee_bps / 10000
    if side == "buy":
        cost = price * quantity + fee
        if cost > cfg.order_budget:
            return "order_budget"
        if cost > dec(state["cash"]) - dec(state["reserved_cash"]):
            return "insufficient_cash"
        # Include pre-existing unfinished bid if this is called independently.
        pending = state.get("order")
        remaining = dec(pending["remaining"]) if pending and pending["side"] == "buy" else dec(0)
        pending_cost = remaining * dec(pending["price"]) * (1 + cfg.fee_bps / 10000) if remaining else dec(0)
        base_after = dec(state["base"]) + remaining + quantity
        equity_after = dec(state["cash"]) - pending_cost - cost + base_after * bid
        if equity_after <= 0 or base_after * bid > cfg.max_position_fraction * equity_after:
            return "projected_exposure"
    elif quantity > dec(state["base"]) - dec(state["reserved_base"]):
        return "insufficient_base"
    return None
