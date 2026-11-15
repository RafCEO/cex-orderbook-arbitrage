"""Connector behavior: parsing, symbol mapping, retries with backoff, rate limiting."""

from __future__ import annotations

import asyncio
import time as time_module
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import TypeVar

import aiohttp
import pytest

from connectors.base import BaseConnector, ConnectorError, RateLimiter
from connectors.binance import BinanceConnector
from connectors.bybit import BybitConnector
from connectors.kraken import KrakenConnector
from tests.fakes import FakeAiohttpSession, FakeResponse, as_client_session, make_level

C = TypeVar("C", bound=BaseConnector)

BINANCE_URL = "https://api.binance.com/api/v3/depth"
KRAKEN_URL = "https://api.kraken.com/0/public/Depth"
BYBIT_URL = "https://api.bybit.com/v5/market/orderbook"


# --- payload fixtures -------------------------------------------------------


def binance_payload(epoch_ms: int | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "lastUpdateId": 123,
        "bids": [["100.5", "2.0"], ["100.0", "3.0"]],
        "asks": [["101.0", "1.5"], ["101.5", "4.0"]],
    }
    if epoch_ms is not None:
        payload["E"] = epoch_ms
    return payload


def kraken_payload() -> dict[str, object]:
    return {
        "error": [],
        "result": {
            "XXBTZUSDT": {
                "bids": [["100.5", "2.0", "2026-01-01T12:00:00.000Z"], ["100.0", "3.0", "x"]],
                "asks": [["101.0", "1.5", "y"], ["101.5", "4.0", "z"]],
            },
            "last": 1704110400,
        },
    }


def bybit_payload(epoch_ms: int = 1767268800000) -> dict[str, object]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "s": "BTCUSDT",
            "b": [["100.5", "2.0"], ["100.0", "3.0"]],
            "a": [["101.0", "1.5"], ["101.5", "4.0"]],
            "ts": epoch_ms,
        },
    }


def make(
    connector_cls: type[C],
    outcomes: list[FakeResponse | BaseException],
    **kwargs: object,
) -> tuple[C, FakeAiohttpSession]:
    session = FakeAiohttpSession(list(outcomes))
    # ignore: the double is cast to ClientSession, kwargs are validated by tests
    conn = connector_cls(session=as_client_session(session), **kwargs)  # type: ignore[arg-type]
    return conn, session


# --- response parsing -------------------------------------------------------


async def test_binance_parses_decimal_levels() -> None:
    conn, _ = make(BinanceConnector, [FakeResponse(payload=binance_payload())])
    snap = await conn.fetch_orderbook("BTC/USDT")
    assert snap.exchange == "binance"
    assert snap.pair == "BTC/USDT"
    assert all(isinstance(lv.price, Decimal) and isinstance(lv.amount, Decimal) for lv in snap.bids)
    assert [lv.price for lv in snap.bids] == [Decimal("100.5"), Decimal("100.0")]
    assert [lv.price for lv in snap.asks] == [Decimal("101.0"), Decimal("101.5")]


async def test_kraken_parses_and_ignores_timestamp_column() -> None:
    conn, _ = make(KrakenConnector, [FakeResponse(payload=kraken_payload())])
    snap = await conn.fetch_orderbook("BTC/USDT")
    assert snap.exchange == "kraken"
    assert [lv.amount for lv in snap.bids] == [Decimal("2.0"), Decimal("3.0")]
    assert [lv.price for lv in snap.asks] == [Decimal("101.0"), Decimal("101.5")]


async def test_bybit_parses_price_size_pairs() -> None:
    conn, _ = make(BybitConnector, [FakeResponse(payload=bybit_payload())])
    snap = await conn.fetch_orderbook("BTC/USDT")
    assert snap.exchange == "bybit"
    assert snap.exchange_ts == datetime.fromtimestamp(1767268800, tz=UTC)
    assert [lv.price for lv in snap.bids] == [Decimal("100.5"), Decimal("100.0")]
    assert [lv.amount for lv in snap.asks] == [Decimal("1.5"), Decimal("4.0")]


# --- symbol mapping ---------------------------------------------------------


def test_binance_symbol_mapping() -> None:
    assert BinanceConnector.to_exchange_symbol("BTC/USDT") == "BTCUSDT"
    assert BinanceConnector.to_canonical_pair("BTCUSDT") == "BTCUSDT"


def test_kraken_symbol_mapping_btc_xbt() -> None:
    assert KrakenConnector.to_exchange_symbol("BTC/USDT") == "XBTUSDT"
    assert KrakenConnector.to_exchange_symbol("ETH/USDT") == "ETHUSDT"


def test_kraken_canonical_pair_modern_and_legacy() -> None:
    assert KrakenConnector.to_canonical_pair("XBTUSDT") == "BTC/USDT"
    assert KrakenConnector.to_canonical_pair("XXBTZUSDT") == "BTC/USDT"
    assert KrakenConnector.to_canonical_pair("ETHUSDT") == "ETH/USDT"
    assert KrakenConnector.to_canonical_pair("XXBTZUSD") == "BTC/USD"


def test_bybit_symbol_mapping() -> None:
    assert BybitConnector.to_exchange_symbol("BTC/USDT") == "BTCUSDT"


# --- request parameter propagation ------------------------------------------


async def test_binance_sends_symbol_and_depth_limit() -> None:
    conn, session = make(BinanceConnector, [FakeResponse(payload=binance_payload())], depth=25)
    await conn.fetch_orderbook("ETH/USDT")
    assert session.calls == [{"url": BINANCE_URL, "params": {"symbol": "ETHUSDT", "limit": "25"}}]


async def test_kraken_sends_pair_and_count() -> None:
    conn, session = make(KrakenConnector, [FakeResponse(payload=kraken_payload())], depth=25)
    await conn.fetch_orderbook("BTC/USDT")
    assert session.calls[0]["url"] == KRAKEN_URL
    assert session.calls[0]["params"] == {"pair": "XBTUSDT", "count": "25"}


async def test_bybit_sends_category_symbol_limit() -> None:
    conn, session = make(BybitConnector, [FakeResponse(payload=bybit_payload())], depth=25)
    await conn.fetch_orderbook("BTC/USDT")
    assert session.calls[0]["url"] == BYBIT_URL
    assert session.calls[0]["params"] == {"category": "spot", "symbol": "BTCUSDT", "limit": "25"}


# --- timestamp fallback behavior ---------------------------------------------


async def test_binance_missing_e_falls_back_to_receive_time() -> None:
    payload = binance_payload()  # no "E" key
    assert "E" not in payload
    conn, _ = make(BinanceConnector, [FakeResponse(payload=payload)])
    snap = await conn.fetch_orderbook("BTC/USDT")
    assert 0.0 <= snap.latency_ms < 100.0  # receive-time fallback: both stamps nearly equal


async def test_binance_e_used_as_exchange_ts() -> None:
    epoch_ms = int(time_module.time() * 1000) - 250
    conn, _ = make(BinanceConnector, [FakeResponse(payload=binance_payload(epoch_ms))])
    snap = await conn.fetch_orderbook("BTC/USDT")
    assert 200.0 < snap.latency_ms < 400.0


async def test_kraken_has_no_server_ts_so_latency_is_near_zero() -> None:
    conn, _ = make(KrakenConnector, [FakeResponse(payload=kraken_payload())])
    snap = await conn.fetch_orderbook("BTC/USDT")
    assert abs(snap.latency_ms) < 100.0  # receive time is the best available estimate


# --- malformed payloads ------------------------------------------------------


async def test_binance_malformed_payload_raises() -> None:
    conn, _ = make(BinanceConnector, [FakeResponse(payload={"foo": 1})])
    with pytest.raises(ValueError, match="malformed"):
        await conn.fetch_orderbook("BTC/USDT")


async def test_kraken_empty_result_raises() -> None:
    conn, _ = make(KrakenConnector, [FakeResponse(payload={"result": {"last": 1}})])
    with pytest.raises(ValueError, match="no depth key"):
        await conn.fetch_orderbook("BTC/USDT")


async def test_bybit_error_code_raises() -> None:
    conn, _ = make(BybitConnector, [FakeResponse(payload={"retCode": 10001, "retMsg": "error!"})])
    with pytest.raises(ValueError, match="bybit"):
        await conn.fetch_orderbook("BTC/USDT")


# --- retry with backoff ------------------------------------------------------


class BackoffHarness:
    """Deterministic retry environment: frozen monotonic clock, recorded sleeps."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sleeps: list[float] = []
        monkeypatch.setattr("connectors.base.random.uniform", lambda low, high: high)
        monkeypatch.setattr("connectors.base.time", SimpleNamespace(monotonic=lambda: 1000.0))

        async def fake_sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        monkeypatch.setattr(
            "connectors.base.asyncio",
            SimpleNamespace(Lock=asyncio.Lock, sleep=fake_sleep),
        )


async def test_retry_growing_delays_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = BackoffHarness(monkeypatch)
    conn, _ = make(
        BinanceConnector,
        [
            FakeResponse(status=503),
            aiohttp.ClientError("boom"),
            FakeResponse(payload=binance_payload()),
        ],
        max_retries=5,
        rate_limit_per_sec=100.0,
    )
    snap = await conn.fetch_orderbook("BTC/USDT")
    assert snap.exchange == "binance"
    # Each failure schedules delay_n (0.5, then 1.0): once as the post-failure
    # sleep, once again as the next attempt's backoff wait (clock is frozen).
    assert harness.sleeps == [0.5, 0.5, 1.0, 1.0]
    assert conn.consecutive_failures == 0
    assert conn.last_error is None


async def test_retry_http_429_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = BackoffHarness(monkeypatch)
    conn, _ = make(
        BinanceConnector,
        [FakeResponse(status=429), FakeResponse(payload=binance_payload())],
        rate_limit_per_sec=100.0,
    )
    snap = await conn.fetch_orderbook("BTC/USDT")
    assert snap.pair == "BTC/USDT"
    assert harness.sleeps == [0.5, 0.5]
    assert conn.consecutive_failures == 0


async def test_retry_exhaustion_raises_connector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = BackoffHarness(monkeypatch)
    conn, _ = make(
        BinanceConnector,
        [FakeResponse(status=503)] * 5,
        max_retries=5,
        rate_limit_per_sec=100.0,
    )
    with pytest.raises(ConnectorError, match="failed after 5 retries"):
        await conn.fetch_orderbook("BTC/USDT")
    assert harness.sleeps == [0.5, 0.5, 1.0, 1.0, 2.0, 2.0, 4.0, 4.0, 8.0]
    assert conn.consecutive_failures == 5
    assert conn.last_error is not None and "HTTP 503" in conn.last_error


async def test_client_error_exhaustion_raises_connector_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    BackoffHarness(monkeypatch)
    conn, _ = make(
        BinanceConnector,
        [aiohttp.ClientConnectionError("reset")] * 5,
        max_retries=5,
        rate_limit_per_sec=100.0,
    )
    with pytest.raises(ConnectorError, match="failed after 5 retries"):
        await conn.fetch_orderbook("BTC/USDT")
    assert conn.consecutive_failures == 5


async def test_os_error_is_not_retried() -> None:
    # Raw OSError escapes the retry budget by design: aiohttp wraps transport
    # errors in ClientError subclasses, so an OSError here is a programming
    # bug the orchestrator (fetch_all) logs rather than a transient failure.
    conn, _ = make(BinanceConnector, [OSError("fd closed")])
    with pytest.raises(OSError):
        await conn.fetch_orderbook("BTC/USDT")


# --- session lifecycle -------------------------------------------------------


async def test_start_rejects_closed_session() -> None:
    session = FakeAiohttpSession()
    session.closed = True
    conn = BinanceConnector(session=as_client_session(session))
    with pytest.raises(ConnectorError, match="session closed"):
        await conn.start()


async def test_close_closes_session() -> None:
    session = FakeAiohttpSession()
    conn = BinanceConnector(session=as_client_session(session))
    await conn.close()
    assert session.closed


# --- rate limiter -------------------------------------------------------------


class FakeMonotonic:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def limiter_harness(
    monkeypatch: pytest.MonkeyPatch, rate: float
) -> tuple[RateLimiter, FakeMonotonic]:
    clock = FakeMonotonic()
    monkeypatch.setattr("connectors.base.time", SimpleNamespace(monotonic=clock))

    async def sleeping_sleep(seconds: float) -> None:
        clock.advance(seconds)

    monkeypatch.setattr(
        "connectors.base.asyncio", SimpleNamespace(Lock=asyncio.Lock, sleep=sleeping_sleep)
    )
    return RateLimiter(rate), clock


async def test_rate_limiter_burst_then_sustained_pace(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter, clock = limiter_harness(monkeypatch, rate=5.0)
    for _ in range(5):  # capacity burst passes instantly
        await limiter.acquire()
    assert clock() == 0.0
    marks = []
    for _ in range(4):  # sustained pace: one token per 1/rate seconds
        await limiter.acquire()
        marks.append(clock())
    assert marks == [pytest.approx(0.2), pytest.approx(0.4), pytest.approx(0.6), pytest.approx(0.8)]


async def test_rate_limiter_respects_window_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter, clock = limiter_harness(monkeypatch, rate=1.0)
    acquire_times: list[float] = []
    for _ in range(5):
        await limiter.acquire()
        acquire_times.append(clock())
    # capacity 1: after the free token, every next acquire waits a full second
    assert acquire_times == [0.0, 1.0, 2.0, 3.0, 4.0]
    for start in acquire_times:
        in_window = [t for t in acquire_times if start <= t < start + 1.0]
        assert len(in_window) <= 1


def test_make_level_factory() -> None:
    lv = make_level("101.5", 2)
    assert lv.price == Decimal("101.5")
    assert lv.amount == Decimal(2)
