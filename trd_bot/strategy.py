"""One fixed non-AI baseline. Strategy proposes an intent; risk alone approves it."""
from .core import dec


def propose(state, book, cfg):
    if cfg.strategy == "no_trade" or state["order"] is not None:
        return None
    bid, bid_size = (dec(x) for x in book["bids"][0][:2])
    ask, ask_size = (dec(x) for x in book["asks"][0][:2])
    imbalance = bid_size / (bid_size + ask_size)
    base = dec(state["base"])
    if base == 0 and imbalance >= cfg.imbalance_buy:
        return {"side": "buy", "price": bid,
                "quantity": cfg.order_budget / (bid * (1 + cfg.fee_bps / 10000))}
    if base > 0 and imbalance <= cfg.imbalance_sell:
        return {"side": "sell", "price": ask, "quantity": base - dec(state["reserved_base"])}
    return None
