"""Public-only Binance transport, exchange rules and ordered depth reconstruction."""
from __future__ import annotations

import asyncio
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
import logging
from pathlib import Path
import time

LOG = logging.getLogger("trd_bot.data")
INFO_URL = "https://data-api.binance.vision/api/v3/exchangeInfo?symbol=BTCUSDT"
DEPTH_URL = "https://data-api.binance.vision/api/v3/depth?symbol=BTCUSDT&limit=5000"
AVG_URL = "https://data-api.binance.vision/api/v3/avgPrice?symbol=BTCUSDT"
WS_URL = "wss://data-stream.binance.vision/stream?streams=btcusdt@depth@100ms/btcusdt@trade"
ALLOWED_URLS = frozenset((INFO_URL, DEPTH_URL, AVG_URL, WS_URL))


def decimal(value):
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise ValueError("Financial values must be decimal strings")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("Non-finite financial value")
    return result


def integer(value):
    if type(value) is not int or value < 0:
        raise ValueError("Nonnegative integer required")
    return value


class MarketRules:
    def __init__(self, envelope):
        self.envelope = json.loads(json.dumps(envelope))
        self.fetched_ms = integer(envelope["fetched_ms"])
        self.source = envelope["source"]
        self.max_age_ms = integer(envelope.get("max_age_ms", 3600000))
        if not self.max_age_ms or not isinstance(self.source, str) or not self.source:
            raise ValueError("Rule provenance and freshness required")
        symbols = envelope["exchangeInfo"]["symbols"]
        if len(symbols) != 1:
            raise ValueError("Exactly one market required")
        symbol = symbols[0]
        if (symbol["symbol"] != "BTCUSDT" or symbol["status"] != "TRADING"
                or not symbol["isSpotTradingAllowed"] or "LIMIT" not in symbol["orderTypes"]):
            raise ValueError("BTCUSDT spot limit market unavailable")
        filters = symbol["filters"]
        self.filters = {f["filterType"]: f for f in filters}
        if len(self.filters) != len(filters):
            raise ValueError("Duplicate filters")
        if not {"PRICE_FILTER", "LOT_SIZE"} <= self.filters.keys():
            raise ValueError("Missing price or lot rules")
        if not {"MIN_NOTIONAL", "NOTIONAL"} & self.filters.keys():
            raise ValueError("Missing notional rules")
        supported = {"PRICE_FILTER", "LOT_SIZE", "MIN_NOTIONAL", "NOTIONAL",
                     "PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE", "MAX_NUM_ORDERS", "MAX_POSITION"}
        inapplicable = {"MARKET_LOT_SIZE", "ICEBERG_PARTS", "MAX_NUM_ICEBERG_ORDERS",
                        "MAX_NUM_ALGO_ORDERS", "TRAILING_DELTA", "MAX_NUM_ORDER_LISTS", "MAX_NUM_ORDER_AMENDS"}
        if self.filters.keys() - supported - inapplicable:
            raise ValueError("Unsupported applicable exchange filter")
        for name, keys in {"PRICE_FILTER": ("minPrice", "maxPrice", "tickSize"),
                           "LOT_SIZE": ("minQty", "maxQty", "stepSize"),
                           "MIN_NOTIONAL": ("minNotional",),
                           "NOTIONAL": ("minNotional", "maxNotional")}.items():
            if name in self.filters:
                for key in keys:
                    if decimal(self.filters[name][key]) < 0:
                        raise ValueError("Negative exchange rule")
        self.tick_size = decimal(self.filters["PRICE_FILTER"]["tickSize"])
        self.step_size = decimal(self.filters["LOT_SIZE"]["stepSize"])
        if self.tick_size <= 0 or self.step_size <= 0:
            raise ValueError("Positive increments required")

    @classmethod
    def from_payload(cls, envelope):
        return cls(envelope)

    @classmethod
    def fixture(cls):
        return cls.from_payload(json.loads((Path(__file__).parent / "fixtures" / "btcusdt-v1.json").read_text(encoding="utf-8")))

    def to_payload(self):
        return json.loads(json.dumps(self.envelope))

    def quantize(self, price, quantity, side):
        if side not in ("buy", "sell"):
            raise ValueError("Unknown side")
        rounding = ROUND_FLOOR if side == "buy" else ROUND_CEILING
        return ((decimal(price) / self.tick_size).to_integral_value(rounding=rounding) * self.tick_size,
                (decimal(quantity) / self.step_size).to_integral_value(rounding=ROUND_FLOOR) * self.step_size)

    def validate(self, price, quantity, side, now_ms):
        price, quantity = decimal(price), decimal(quantity)
        now_ms = integer(now_ms)
        if side not in ("buy", "sell") or price <= 0 or quantity <= 0:
            raise ValueError("Invalid limit order")
        if not 0 <= now_ms - self.fetched_ms <= self.max_age_ms:
            raise ValueError("Unknown or expired market rules")
        for name, value, low, high, step in (("PRICE_FILTER", price, "minPrice", "maxPrice", "tickSize"),
                                            ("LOT_SIZE", quantity, "minQty", "maxQty", "stepSize")):
            f = self.filters[name]
            minimum, maximum, increment = (decimal(f[k]) for k in (low, high, step))
            if (minimum and value < minimum) or (maximum and value > maximum) or (increment and value % increment):
                raise ValueError(name)
        notional = price * quantity
        for name in ("MIN_NOTIONAL", "NOTIONAL"):
            if name in self.filters:
                f = self.filters[name]
                if notional < decimal(f["minNotional"]) or (name == "NOTIONAL" and notional > decimal(f["maxNotional"])):
                    raise ValueError(name)
        for name in ("PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE"):
            if name not in self.filters:
                continue
            f = self.filters[name]
            reference = self.envelope.get("avg_price")
            if not reference or reference.get("mins") != f["avgPriceMins"]:
                raise ValueError("Unsupported average-price interval")
            if not 0 <= now_ms - integer(reference["timestamp"]) <= 5000:
                raise ValueError("Stale average-price reference")
            average = decimal(reference["price"])
            prefix = ("bid" if side == "buy" else "ask") if name.endswith("BY_SIDE") else ""
            low = decimal(f[prefix + "MultiplierDown" if prefix else "multiplierDown"])
            high = decimal(f[prefix + "MultiplierUp" if prefix else "multiplierUp"])
            if average <= 0 or not average * low <= price <= average * high:
                raise ValueError(name)
        # The engine permits only one outstanding order; reject exchange limits below that.
        if "MAX_NUM_ORDERS" in self.filters and integer(self.filters["MAX_NUM_ORDERS"]["maxNumOrders"]) < 1:
            raise ValueError("MAX_NUM_ORDERS")
        if "MAX_POSITION" in self.filters:
            raise ValueError("MAX_POSITION requires account context")


class AllowlistedTransport:
    @staticmethod
    def validate_url(url, method="GET"):
        if method != "GET" or str(url) not in ALLOWED_URLS:
            raise ValueError("Network endpoint forbidden")

    async def __aenter__(self):
        import aiohttp
        trace = aiohttp.TraceConfig()

        async def reject_redirect(*args):
            raise ValueError("Network redirects forbidden")

        trace.on_request_redirect.append(reject_redirect)
        self.session = aiohttp.ClientSession(trust_env=False, trace_configs=[trace],
                                             timeout=aiohttp.ClientTimeout(total=10))
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def get_json(self, url):
        self.validate_url(url)
        if url == WS_URL:
            raise ValueError("WebSocket URL is not REST")
        async with self.session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError(f"Public HTTP status {response.status}")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > 2_000_000:
                    raise ValueError("Public response too large")
            return json.loads(body)

    async def messages(self, url):
        import aiohttp
        self.validate_url(url)
        if url != WS_URL:
            raise ValueError("REST URL is not WebSocket")
        async with self.session.ws_connect(url, autoping=True, max_msg_size=1_000_000) as socket:
            async for message in socket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    raise ValueError("Public stream failed")
                yield json.loads(message.data)
        raise ValueError("Public stream disconnected")


async def fetch_rules(transport, now_ms):
    raw = await transport.get_json(INFO_URL)
    average = await transport.get_json(AVG_URL)
    return MarketRules.from_payload({"schema": 1, "source": INFO_URL, "fetched_ms": now_ms,
        "max_age_ms": 3600000, "exchangeInfo": raw,
        "avg_price": {"price": average["price"], "mins": average["mins"],
                      "timestamp": integer(average["closeTime"])}})


async def public_events(transport, emit):
    """One bounded ordered stream; gaps, overflow and disconnect propagate to risk halt."""
    queue = asyncio.Queue(maxsize=1024)
    max_age_ms = getattr(transport, "max_data_age_ms", 5000)

    async def reader():
        async for message in transport.messages(WS_URL):
            queue.put_nowait((message, int(time.time() * 1000)))
        raise ValueError("Public stream disconnected")

    task = asyncio.create_task(reader())
    try:
        # Subscribe before obtaining the snapshot, buffering deltas during REST I/O.
        first = await asyncio.wait_for(queue.get(), max_age_ms / 1000)
        snapshot = await transport.get_json(DEPTH_URL)
        nonce = integer(snapshot["lastUpdateId"])
        book = []
        for name in ("bids", "asks"):
            levels = {}
            for p, q in snapshot[name]:
                p, q = decimal(p), decimal(q)
                if p <= 0 or q <= 0 or p in levels:
                    raise ValueError("Invalid depth snapshot")
                levels[p] = q
            book.append(levels)
        pending = first
        while True:
            if task.done():
                task.result()
            if pending is None:
                try:
                    pending = await asyncio.wait_for(queue.get(), .1)
                except asyncio.TimeoutError:
                    continue
            message, received = pending
            pending = None
            stream, event = message["stream"], message["data"]
            if event.get("s") != "BTCUSDT":
                raise ValueError("Unexpected stream symbol")
            timestamp = integer(event["E"])
            if timestamp > received + 1000 or received - timestamp > max_age_ms:
                raise ValueError("Invalid source time")
            common = {"timestamp": timestamp, "received_ms": received,
                      "source": "binance_public", "connection": "connected"}
            if stream == "btcusdt@depth@100ms" and event["e"] == "depthUpdate":
                start, end = integer(event["U"]), integer(event["u"])
                if start > end:
                    raise ValueError("Invalid depth sequence")
                if end <= nonce:
                    continue
                if start > nonce + 1:
                    raise ValueError("Depth sequence gap")
                for levels, key in zip(book, ("b", "a")):
                    for p, q in event[key]:
                        p, q = decimal(p), decimal(q)
                        if p <= 0 or q < 0:
                            raise ValueError("Invalid depth delta")
                        if q:
                            levels[p] = q
                        else:
                            levels.pop(p, None)
                    if not levels or len(levels) > 20000:
                        raise ValueError("Incomplete or oversized depth")
                nonce = end
                bids = sorted(book[0].items(), reverse=True)[:20]
                asks = sorted(book[1].items())[:20]
                if bids[0][0] >= asks[0][0]:
                    raise ValueError("Crossed depth")
                emit({**common, "type": "book", "id": f"depth:{end}",
                      "bids": [[str(p), str(q)] for p, q in bids],
                      "asks": [[str(p), str(q)] for p, q in asks]})
            elif stream == "btcusdt@trade" and event["e"] == "trade":
                trade_time = integer(event["T"])
                if type(event["m"]) is not bool or trade_time > timestamp or timestamp - trade_time > max_age_ms:
                    raise ValueError("Invalid trade metadata")
                p, q = decimal(event["p"]), decimal(event["q"])
                if p <= 0 or q <= 0:
                    raise ValueError("Invalid trade values")
                emit({**common, "timestamp": trade_time, "type": "trade",
                      "id": f"trade:{integer(event['t'])}", "side": "sell" if event["m"] else "buy",
                      "price": str(p), "amount": str(q)})
            else:
                raise ValueError("Unexpected public event")
    except Exception:
        LOG.exception("Public market stream stopped")
        raise
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
