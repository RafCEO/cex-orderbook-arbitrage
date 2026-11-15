"""LiveExecutor: request construction, gate logic, error surfacing (hermetic)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from execution.auth import binance_signature
from execution.base import ExecutionStatus, OrderIntent
from execution.live_executor import (
    BYBIT_LIVE_URL,
    BYBIT_TESTNET_URL,
    LiveExecutor,
    exchange_base_urls,
    load_credentials,
)
from tests.fakes import FakeAiohttpSession, FakeResponse, as_client_session

CREDS = {"binance": ("binance-key", "binance-secret")}
BOTH_CREDS = {
    "binance": ("binance-key", "binance-secret"),
    "bybit": ("bybit-key", "bybit-secret"),
}


def make_intent(**overrides: object) -> OrderIntent:
    defaults: dict[str, object] = {
        "pair": "BTC/USDT",
        "buy_exchange": "binance",
        "sell_exchange": "bybit",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "budget_quote": Decimal("100"),
        "expected_buy_vwap": Decimal("50000"),
        "expected_sell_vwap": Decimal("50100"),
        "expected_net_spread_bps": Decimal("10"),
    }
    defaults.update(overrides)
    return OrderIntent(**defaults)  # type: ignore[arg-type]


def make_executor(
    mode: str = "testnet",
    outcomes: list[FakeResponse | BaseException] | None = None,
    creds: dict[str, tuple[str, str]] | None = None,
) -> tuple[LiveExecutor, FakeAiohttpSession]:
    session = FakeAiohttpSession(outcomes)
    executor = LiveExecutor(
        session=as_client_session(session),
        mode=mode,
        credentials=creds if creds is not None else BOTH_CREDS,
    )
    return executor, session


# -- construction gates ---------------------------------------------------------


def test_base_urls_per_mode() -> None:
    assert exchange_base_urls("live")["binance"] == "https://api.binance.com"
    assert exchange_base_urls("live")["bybit"] == BYBIT_LIVE_URL
    assert exchange_base_urls("testnet")["binance"] == "https://testnet.binance.vision"
    assert exchange_base_urls("testnet")["bybit"] == BYBIT_TESTNET_URL


def test_construction_rejects_invalid_mode() -> None:
    session = FakeAiohttpSession()
    with pytest.raises(ValueError, match="testnet or live"):
        LiveExecutor(session=as_client_session(session), mode="paper", credentials=CREDS)


def test_construction_refuses_without_credentials() -> None:
    session = FakeAiohttpSession()
    with pytest.raises(Exception, match="environment"):
        LiveExecutor(session=as_client_session(session), mode="live", credentials={})


def test_load_credentials_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BINANCE_API_KEY", "bk")
    monkeypatch.setenv("BINANCE_API_SECRET", "bs")
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    creds = load_credentials("testnet")
    assert creds == {"binance": ("bk", "bs")}


def test_load_credentials_ignores_partial_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    assert load_credentials("live") == {}


def test_supports_only_credentialed_exchanges() -> None:
    executor, _ = make_executor(creds=CREDS)
    assert executor.supports("binance")
    assert not executor.supports("bybit")


# -- request construction ----------------------------------------------------------


def binance_fill_payload(executed: str = "0.002", quote: str = "100") -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "executedQty": executed,
        "cummulativeQuoteQty": quote,
        "status": "FILLED",
    }


def bybit_fill_payload(executed: str = "0.002", value: str = "100") -> dict[str, object]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "orderId": "1",
            "cumulativeExecutedQty": executed,
            "cumulativeExecutedValue": value,
        },
    }


async def test_execute_uses_testnet_urls_in_testnet_mode() -> None:
    executor, session = make_executor(
        outcomes=[
            FakeResponse(payload=binance_fill_payload()),
            FakeResponse(payload=bybit_fill_payload()),
        ]
    )
    report = await executor.execute(make_intent())
    assert report.status == ExecutionStatus.FILLED
    urls = [c["url"] for c in session.calls]
    assert urls[0].startswith("https://testnet.binance.vision")
    assert urls[1].startswith(BYBIT_TESTNET_URL)


async def test_execute_uses_live_urls_in_live_mode() -> None:
    executor, session = make_executor(
        mode="live",
        outcomes=[
            FakeResponse(payload=binance_fill_payload()),
            FakeResponse(payload=bybit_fill_payload()),
        ],
    )
    report = await executor.execute(make_intent())
    assert report.status in (ExecutionStatus.FILLED, ExecutionStatus.PARTIAL)
    urls = [c["url"] for c in session.calls]
    assert urls[0].startswith("https://api.binance.com")
    assert urls[1].startswith(BYBIT_LIVE_URL)


async def test_binance_order_params_include_ioc_limit_and_signature() -> None:
    executor, session = make_executor(
        outcomes=[
            FakeResponse(payload=binance_fill_payload()),
            FakeResponse(payload=bybit_fill_payload()),
        ]
    )
    await executor.execute(make_intent())
    buy_call = session.calls[0]
    assert buy_call["url"].endswith("/api/v3/order")
    params = buy_call["params"]
    assert params["symbol"] == "BTCUSDT"
    assert params["side"] == "BUY"
    assert params["type"] == "LIMIT"
    assert params["timeInForce"] == "IOC"
    assert "price" in params and "timestamp" in params and "signature" in params
    headers = buy_call["headers"]
    assert headers["X-MBX-APIKEY"] == "binance-key"


async def test_binance_signature_covers_payload_params() -> None:
    executor, session = make_executor(
        outcomes=[
            FakeResponse(payload=binance_fill_payload()),
            FakeResponse(payload=bybit_fill_payload()),
        ]
    )
    await executor.execute(make_intent())
    params = dict(session.calls[0]["params"])
    signature = params.pop("signature")
    from urllib.parse import urlencode

    query = urlencode(params)
    assert signature == binance_signature("binance-secret", query)


async def test_bybit_order_headers_and_query() -> None:
    executor, session = make_executor(
        outcomes=[
            FakeResponse(payload=binance_fill_payload()),
            FakeResponse(payload=bybit_fill_payload()),
        ]
    )
    await executor.execute(make_intent())
    sell_call = session.calls[1]
    assert "/v5/order/create" in sell_call["url"]
    assert "symbol=BTCUSDT" in sell_call["url"]
    assert "timeInForce=IOC" in sell_call["url"]
    headers = sell_call["headers"]
    assert headers["X-BAPI-APIKEY"] == "bybit-key"
    assert "X-BAPI-SIGN" in headers and "X-BAPI-TIMESTAMP" in headers


async def test_market_orders_escape_hatch_omits_limit_params() -> None:
    session = FakeAiohttpSession(
        [
            FakeResponse(payload=binance_fill_payload()),
            FakeResponse(payload=bybit_fill_payload()),
        ]
    )
    executor = LiveExecutor(
        session=as_client_session(session),
        mode="testnet",
        credentials=BOTH_CREDS,
        market_orders=True,
    )
    await executor.execute(make_intent())
    assert session.calls[0]["params"]["type"] == "MARKET"
    assert "price" not in session.calls[0]["params"]
    assert "timeInForce" not in session.calls[0]["params"]


async def test_price_buffer_pads_buy_up_and_sell_down() -> None:
    executor, _ = make_executor()
    buy = executor._protected_price(Decimal("100"), "buy")
    sell = executor._protected_price(Decimal("100"), "sell")
    assert Decimal(buy) > Decimal("100")
    assert Decimal(sell) < Decimal("100")
    # default 5 bps
    assert Decimal(buy) == pytest.approx(Decimal("100.05"), abs=Decimal("1e-6"))
    assert Decimal(sell) == pytest.approx(Decimal("99.95"), abs=Decimal("1e-6"))


async def test_missing_credentials_route_rejected() -> None:
    executor, _ = make_executor(creds=CREDS)
    report = await executor.execute(make_intent(sell_exchange="bybit"))
    assert report.status == ExecutionStatus.REJECTED
    assert "no credentials" in (report.reason or "")


async def test_kraken_route_rejected_with_documented_reason() -> None:
    executor, _ = make_executor()
    report = await executor.execute(make_intent(sell_exchange="kraken"))
    assert report.status == ExecutionStatus.REJECTED


async def test_http_error_surfaces_as_rejected_report() -> None:
    executor, _ = make_executor(outcomes=[FakeResponse(status=401, payload={"msg": "bad key"})])
    report = await executor.execute(make_intent())
    assert report.status == ExecutionStatus.REJECTED
    assert "401" in (report.reason or "")


async def test_bybit_retcode_error_surfaces() -> None:
    executor, _ = make_executor(
        outcomes=[
            FakeResponse(payload=binance_fill_payload()),
            FakeResponse(payload={"retCode": 10001, "retMsg": "order failed"}),
        ]
    )
    report = await executor.execute(make_intent())
    assert report.status == ExecutionStatus.REJECTED
    assert "bybit" in (report.reason or "")


async def test_zero_fill_treated_as_error_not_fake_fill() -> None:
    executor, _ = make_executor(
        outcomes=[FakeResponse(payload=binance_fill_payload(executed="0", quote="0"))]
    )
    report = await executor.execute(make_intent())
    assert report.status == ExecutionStatus.REJECTED
    assert report.buy_fill is None


async def test_secrets_never_appear_in_reports_or_calls() -> None:
    executor, session = make_executor(
        outcomes=[
            FakeResponse(payload=binance_fill_payload()),
            FakeResponse(payload=bybit_fill_payload()),
        ]
    )
    report = await executor.execute(make_intent())
    blob = str(report.model_dump()) + str(session.calls)
    assert "binance-secret" not in blob
    assert "bybit-secret" not in blob


async def test_balances_fetch_parses_both_exchanges() -> None:
    executor, session = make_executor(
        outcomes=[
            FakeResponse(
                payload={
                    "balances": [{"asset": "USDT", "free": "10.5"}, {"asset": "BTC", "free": "0"}]
                }
            ),
            FakeResponse(
                payload={
                    "result": {"list": [{"coin": [{"coin": "USDT", "walletBalance": "8.25"}]}]}
                }
            ),
        ]
    )
    balances = await executor.balances()
    assert balances["binance"] == {"USDT": Decimal("10.5")}
    assert balances["bybit"] == {"USDT": Decimal("8.25")}
