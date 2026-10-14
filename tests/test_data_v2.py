import asyncio
from decimal import Decimal
import time

import pytest

from trd_bot.data import (AllowlistedTransport, MarketRules, INFO_URL, AVG_URL,
                          DEPTH_URL, WS_URL, fetch_rules, public_events)


def test_rules_decimal_boundaries_and_roundtrip():
    rules = MarketRules.fixture()
    assert MarketRules.from_payload(rules.to_payload()).tick_size == Decimal("0.01")
    assert rules.quantize("50000.019", "0.000209", "buy") == (Decimal("50000.01"), Decimal("0.00020"))
    assert rules.quantize("50000.019", "0.000209", "sell")[0] == Decimal("50000.02")
    rules.validate("50000", "0.0001", "buy", rules.fetched_ms)
    payload = rules.to_payload()
    payload["exchangeInfo"]["symbols"][0]["filters"].extend([
        {"filterType":"MAX_NUM_ORDER_LISTS","maxNumOrderLists":20},
        {"filterType":"MAX_NUM_ORDER_AMENDS","maxNumOrderAmends":10}])
    MarketRules.from_payload(payload).validate("50000", "0.0001", "buy", rules.fetched_ms)
    for price, qty, now in (("50000.001", ".0001", rules.fetched_ms),
                            ("50000", ".00009", rules.fetched_ms),
                            ("50000", ".000101", rules.fetched_ms),
                            ("50000", ".0001", rules.fetched_ms + 3600001)):
        with pytest.raises(ValueError):
            rules.validate(price, qty, "buy", now)
    with pytest.raises(ValueError):
        rules.quantize(50000.0, ".0001", "buy")


def test_percent_reference_and_unknown_filters_fail_closed():
    payload = MarketRules.fixture().to_payload()
    payload["exchangeInfo"]["symbols"][0]["filters"].append({
        "filterType": "PERCENT_PRICE_BY_SIDE", "avgPriceMins": 5,
        "bidMultiplierDown": ".9", "bidMultiplierUp": "1.1",
        "askMultiplierDown": ".8", "askMultiplierUp": "1.2"})
    now = payload["fetched_ms"]
    with pytest.raises(ValueError):
        MarketRules.from_payload(payload).validate("50000", ".0002", "buy", now)
    payload["avg_price"] = {"price": "50000", "mins": 5, "timestamp": now}
    rules = MarketRules.from_payload(payload)
    rules.validate("50000", ".0002", "buy", now)
    with pytest.raises(ValueError):
        rules.validate("60000", ".0002", "buy", now)
    with pytest.raises(ValueError):
        rules.validate("50000", ".0002", "buy", now + 5001)
    payload["exchangeInfo"]["symbols"][0]["filters"].append({"filterType": "NEW_RULE"})
    with pytest.raises(ValueError):
        MarketRules.from_payload(payload)


def test_transport_allowlist_before_fake_network():
    for url in (INFO_URL, AVG_URL, DEPTH_URL, WS_URL):
        AllowlistedTransport.validate_url(url)
    for url in ("https://api.binance.com/api/v3/order", INFO_URL + "&extra=1",
                INFO_URL.replace("https:", "http:"), INFO_URL.replace("data-api.", "evil@data-api."),
                "wss://data-stream.binance.vision/ws/btcusdt@depth20@100ms"):
        with pytest.raises(ValueError):
            AllowlistedTransport.validate_url(url)
    with pytest.raises(ValueError):
        AllowlistedTransport.validate_url(INFO_URL, "POST")


def test_fake_fetch_rules():
    class Fake:
        async def get_json(self, url):
            AllowlistedTransport.validate_url(url)
            if url == INFO_URL:
                return MarketRules.fixture().to_payload()["exchangeInfo"]
            assert url == AVG_URL
            return {"price": "50000", "mins": 5, "closeTime": 1700000000000}
    rules = asyncio.run(fetch_rules(Fake(), 1700000000000))
    assert rules.source == INFO_URL


def test_fake_transport_redirect_guard_and_no_auth(monkeypatch):
    import aiohttp
    captured = {}
    class Session:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        async def close(self):
            captured["closed"] = True
    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    async def run():
        async with AllowlistedTransport() as transport:
            assert captured["trust_env"] is False
            assert "auth" not in captured and "headers" not in captured
            guard = captured["trace_configs"][0].on_request_redirect[0]
            with pytest.raises(ValueError, match="redirects forbidden"):
                await guard(None, None, None)
            with pytest.raises(ValueError, match="endpoint forbidden"):
                await transport.get_json("https://data-api.binance.vision/api/v3/account")
        assert captured["closed"]
    asyncio.run(run())


def test_diff_bootstrap_trade_and_gap():
    async def run():
        now = int(time.time() * 1000)
        delta = {"e": "depthUpdate", "E": now, "s": "BTCUSDT", "U": 11, "u": 12,
                 "b": [["49999", "2"]], "a": []}
        messages = [{"stream": "btcusdt@depth@100ms", "data": delta},
                    {"stream": "btcusdt@trade", "data": {"e": "trade", "E": now, "T": now,
                     "s": "BTCUSDT", "t": 1, "m": True, "p": "49999", "q": ".01"}},
                    {"stream": "btcusdt@depth@100ms", "data": {**delta, "U": 15, "u": 16}}]
        class Fake:
            async def get_json(self, url):
                assert url == DEPTH_URL
                return {"lastUpdateId": 10, "bids": [["49999", "1"]], "asks": [["50001", "1"]]}
            async def messages(self, url):
                assert url == WS_URL
                for message in messages:
                    yield message
                    await asyncio.sleep(.001)
                await asyncio.Event().wait()
        output = []
        with pytest.raises(ValueError, match="sequence gap"):
            await public_events(Fake(), output.append)
        assert [x["type"] for x in output] == ["book", "trade"]
        assert output[0]["bids"][0] == ["49999", "2"]
        assert output[1]["side"] == "sell"
    asyncio.run(run())


def test_missing_timestamp_and_overflow_stop():
    async def run(overflow):
        now = int(time.time() * 1000)
        class Fake:
            async def get_json(self, url):
                return {"lastUpdateId": 0, "bids": [["1", "1"]], "asks": [["2", "1"]]}
            async def messages(self, url):
                if overflow:
                    for _ in range(1026):
                        yield {}
                else:
                    yield {"stream": "btcusdt@depth@100ms", "data": {"s": "BTCUSDT"}}
                    await asyncio.Event().wait()
        with pytest.raises((KeyError, asyncio.QueueFull)):
            await public_events(Fake(), lambda e: None)
    asyncio.run(run(False))
    asyncio.run(run(True))
