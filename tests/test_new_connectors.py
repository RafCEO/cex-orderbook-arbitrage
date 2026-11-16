"""New connectors: payload parsing, symbol mapping, timestamps, depth slicing."""

from __future__ import annotations

import time as time_module
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar

import pytest

from connectors.base import BaseConnector
from connectors.binance import BinanceConnector
from connectors.bitbank import BitbankConnector
from connectors.bitflyer import BitflyerConnector
from connectors.bybit import BybitConnector
from connectors.coincheck import CoincheckConnector
from connectors.gate import GateConnector
from connectors.kraken import KrakenConnector
from connectors.kucoin import KucoinConnector
from connectors.okx import OkxConnector
from tests.fakes import FakeAiohttpSession, FakeResponse, as_client_session

C = TypeVar("C", bound=BaseConnector)

OKX_URL = "https://www.okx.com/api/v5/market/books"
KUCOIN_URL = "https://api.kucoin.com/api/v1/market/orderbook/level2_100"
GATE_URL = "https://api.gateio.ws/api/v4/spot/order_book"
BITFLYER_URL = "https://api.bitflyer.com/v1/board"
BITBANK_URL = "https://public.bitbank.cc"
COINCHECK_URL = "https://coincheck.com/api/order_books"


def make(
    connector_cls: type[C],
    outcomes: list[FakeResponse | BaseException],
    **kwargs: object,
) -> tuple[C, FakeAiohttpSession]:
    session = FakeAiohttpSession(list(outcomes))
    # ignore: the double is cast to ClientSession, kwargs are validated by tests
    conn = connector_cls(session=as_client_session(session), **kwargs)  # type: ignore[arg-type]
    return conn, session


def okx_payload(ts: str | None = "1794837015204") -> dict[str, object]:
    book: dict[str, object] = {
        "asks": [["64391.8", "0.19894912", "0", "3"], ["64392", "0.06", "0", "1"]],
        "bids": [["64390.9", "0.61692142", "0", "7"], ["64390.5", "0.06108186", "0", "2"]],
    }
    if ts is not None:
        book["ts"] = ts
    return {"code": "0", "msg": "", "data": [book]}


def kucoin_payload(ts: int | None = 1794837044232, levels: int = 6) -> dict[str, object]:
    data: dict[str, object] = {
        "time": ts,
        "sequence": "40542155167",
        "bids": [[f"6437{8 - i}.8", "0.05053349"] for i in range(levels)],
        "asks": [[f"6438{i + 1}.1", "0.01554665"] for i in range(levels)],
    }
    return {"code": "200000", "data": data}


def gate_payload(updated: int | None = 1794837045016) -> dict[str, object]:
    payload: dict[str, object] = {
        "current": 1794837045020,
        "asks": [["64388.7", "0.290911"], ["64391.9", "0.000158"]],
        "bids": [["64388.6", "0.059078"], ["64387.1", "0.0002"]],
    }
    if updated is not None:
        payload["update"] = updated
    return payload


def bitflyer_payload() -> dict[str, object]:
    return {
        "mid_price": 10273732.0,
        "bids": [{"price": 10272340.0, "size": 0.00119839}, {"price": 10271814.0, "size": 0.5}],
        "asks": [{"price": 10274500.0, "size": 0.0049132}, {"price": 10275000.0, "size": 1.0}],
    }


def bitbank_payload(ts: int | None = 1794837060703) -> dict[str, object]:
    data: dict[str, object] = {
        "asks": [["10270787", "0.0196", "a"], ["10273060", "0.0008", "a"]],
        "bids": [["10270786", "0.1137", "b"], ["10270700", "0.5", "b"]],
        "asks_over": [],
        "bids_under": [],
        "sequenceId": "123",
    }
    if ts is not None:
        data["timestamp"] = ts
    return {"success": 1, "data": data}


def coincheck_payload() -> dict[str, object]:
    return {
        "asks": [["10269907.0", "0.04"], ["10270230.0", "0.053"]],
        "bids": [["10262000.0", "0.02487785"], ["10261784.0", "0.01"]],
    }


# --- parsing -----------------------------------------------------------------


async def test_okx_parses_decimal_levels_ignoring_liq_columns() -> None:
    conn, _ = make(OkxConnector, [FakeResponse(payload=okx_payload())])
    snap = await conn.fetch_orderbook("BTC/USDT")
    assert snap.exchange == "okx"
    assert all(isinstance(lv.price, Decimal) for lv in snap.bids)
    assert [lv.price for lv in snap.bids] == [Decimal("64390.9"), Decimal("64390.5")]
    assert [lv.amount for lv in snap.asks] == [Decimal("0.19894912"), Decimal("0.06")]
    assert snap.exchange_ts == datetime.fromtimestamp(1794837015.204, tz=UTC)


async def test_kucoin_parses_string_pairs_and_slices_depth() -> None:
    conn, session = make(KucoinConnector, [FakeResponse(payload=kucoin_payload())], depth=5)
    snap = await conn.fetch_orderbook("BTC/USDT")
    assert snap.exchange == "kucoin"
    assert len(snap.bids) == 5  # sliced from 6 raw levels
    assert len(snap.asks) == 5
    assert session.calls[0]["params"] == {"symbol": "BTC-USDT"}
    assert snap.exchange_ts == datetime.fromtimestamp(1794837044.232, tz=UTC)


async def test_gate_parses_decimal_levels_and_update_ts() -> None:
    conn, _ = make(GateConnector, [FakeResponse(payload=gate_payload())])
    snap = await conn.fetch_orderbook("BTC/USDT")
    assert snap.exchange == "gate"
    assert [lv.price for lv in snap.bids] == [Decimal("64388.6"), Decimal("64387.1")]
    assert [lv.price for lv in snap.asks] == [Decimal("64388.7"), Decimal("64391.9")]
    assert snap.exchange_ts == datetime.fromtimestamp(1794837045.016, tz=UTC)


async def test_bitflyer_parses_numbers_as_exact_decimals() -> None:
    conn, _ = make(BitflyerConnector, [FakeResponse(payload=bitflyer_payload())])
    snap = await conn.fetch_orderbook("BTC/JPY")
    assert snap.exchange == "bitflyer"
    assert [lv.price for lv in snap.bids] == [Decimal("10272340.0"), Decimal("10271814.0")]
    assert [lv.amount for lv in snap.asks] == [Decimal("0.0049132"), Decimal("1.0")]
    # no server timestamp: receive time doubles as exchange_ts
    assert abs(snap.latency_ms) < 100.0


async def test_bitbank_parses_levels_and_server_timestamp() -> None:
    conn, session = make(BitbankConnector, [FakeResponse(payload=bitbank_payload())])
    snap = await conn.fetch_orderbook("BTC/JPY")
    assert snap.exchange == "bitbank"
    assert [lv.price for lv in snap.bids] == [Decimal("10270786"), Decimal("10270700")]
    assert [lv.amount for lv in snap.asks] == [Decimal("0.0196"), Decimal("0.0008")]
    assert snap.exchange_ts == datetime.fromtimestamp(1794837060.703, tz=UTC)
    assert session.calls[0]["url"] == f"{BITBANK_URL}/btc_jpy/depth"


async def test_coincheck_parses_unsorted_and_model_sorts() -> None:
    payload = coincheck_payload()
    asks: list[list[str]] = payload["asks"]  # type: ignore[assignment]
    bids: list[list[str]] = payload["bids"]  # type: ignore[assignment]
    payload["asks"] = asks[::-1]
    payload["bids"] = bids[::-1]
    conn, _ = make(CoincheckConnector, [FakeResponse(payload=payload)])
    snap = await conn.fetch_orderbook("BTC/JPY")
    assert snap.exchange == "coincheck"
    assert [lv.price for lv in snap.asks] == sorted(lv.price for lv in snap.asks)
    assert [lv.price for lv in snap.bids] == sorted((lv.price for lv in snap.bids), reverse=True)
    assert abs(snap.latency_ms) < 100.0  # receive-time fallback


# --- symbol mapping ----------------------------------------------------------


def test_okx_symbol_mapping_both_directions() -> None:
    assert OkxConnector.to_exchange_symbol("BTC/USDT") == "BTC-USDT"
    assert OkxConnector.to_canonical_pair("ETH-USDT") == "ETH/USDT"


def test_kucoin_symbol_mapping_both_directions() -> None:
    assert KucoinConnector.to_exchange_symbol("BTC/USDT") == "BTC-USDT"
    assert KucoinConnector.to_canonical_pair("BTC-USDT") == "BTC/USDT"


def test_gate_symbol_mapping_both_directions() -> None:
    assert GateConnector.to_exchange_symbol("BTC/USDT") == "BTC_USDT"
    assert GateConnector.to_canonical_pair("BTC_USDT") == "BTC/USDT"


def test_bitflyer_symbol_mapping_both_directions() -> None:
    assert BitflyerConnector.to_exchange_symbol("BTC/JPY") == "BTC_JPY"
    assert BitflyerConnector.to_canonical_pair("BTC_JPY") == "BTC/JPY"


def test_bitbank_symbol_mapping_lower_case_path() -> None:
    assert BitbankConnector.to_exchange_symbol("BTC/JPY") == "btc_jpy"
    assert BitbankConnector.to_canonical_pair("btc_jpy") == "BTC/JPY"


def test_coincheck_symbol_mapping_lower_case_pair() -> None:
    assert CoincheckConnector.to_exchange_symbol("ETH/JPY") == "eth_jpy"
    assert CoincheckConnector.to_canonical_pair("eth_jpy") == "ETH/JPY"


# --- timestamp fallbacks -------------------------------------------------------


async def test_okx_missing_ts_falls_back_to_receive_time() -> None:
    conn, _ = make(OkxConnector, [FakeResponse(payload=okx_payload(ts=None))])
    snap = await conn.fetch_orderbook("BTC/USDT")
    assert abs(snap.latency_ms) < 100.0


async def test_okx_ts_drives_latency() -> None:
    epoch_ms = int(time_module.time() * 1000) - 250
    conn, _ = make(OkxConnector, [FakeResponse(payload=okx_payload(ts=str(epoch_ms)))])
    snap = await conn.fetch_orderbook("BTC/USDT")
    assert 150.0 < snap.latency_ms < 400.0


async def test_kucoin_missing_time_falls_back_to_receive_time() -> None:
    conn, _ = make(KucoinConnector, [FakeResponse(payload=kucoin_payload(ts=None))])
    snap = await conn.fetch_orderbook("BTC/USDT")
    assert abs(snap.latency_ms) < 100.0


async def test_gate_missing_update_uses_current_then_receive_time() -> None:
    conn, _ = make(GateConnector, [FakeResponse(payload=gate_payload(updated=None))])
    snap = await conn.fetch_orderbook("BTC/USDT")
    # current=1794837045020 ms drives the stamp even without update
    assert snap.exchange_ts == datetime.fromtimestamp(1794837045.02, tz=UTC)


async def test_bitbank_missing_timestamp_falls_back_to_receive_time() -> None:
    conn, _ = make(BitbankConnector, [FakeResponse(payload=bitbank_payload(ts=None))])
    snap = await conn.fetch_orderbook("BTC/JPY")
    assert abs(snap.latency_ms) < 100.0


# --- malformed payloads --------------------------------------------------------


async def test_okx_error_code_raises() -> None:
    conn, _ = make(
        OkxConnector,
        [FakeResponse(payload={"code": "51001", "msg": "Instrument ID does not exist."})],
    )
    with pytest.raises(ValueError, match="okx"):
        await conn.fetch_orderbook("BTC/USDT")


async def test_kucoin_malformed_raises() -> None:
    conn, _ = make(KucoinConnector, [FakeResponse(payload={"code": "404", "msg": "not exist"})])
    with pytest.raises(ValueError, match="kucoin"):
        await conn.fetch_orderbook("BTC/USDT")


async def test_gate_malformed_raises() -> None:
    conn, _ = make(GateConnector, [FakeResponse(payload={"label": "INVALID_CURRENCY_PAIR"})])
    with pytest.raises(ValueError, match="gate"):
        await conn.fetch_orderbook("BTC/JPY")


async def test_bitflyer_error_status_raises() -> None:
    conn, _ = make(
        BitflyerConnector,
        [FakeResponse(payload={"status": -100, "error_message": "Invalid product", "data": None})],
    )
    with pytest.raises(ValueError, match="bitflyer"):
        await conn.fetch_orderbook("BTC/USDT")


async def test_bitbank_error_raises() -> None:
    conn, _ = make(
        BitbankConnector, [FakeResponse(payload={"success": 0, "data": {"code": 10000}})]
    )
    with pytest.raises(ValueError, match="bitbank"):
        await conn.fetch_orderbook("BTC/JPY")


async def test_coincheck_malformed_raises() -> None:
    conn, _ = make(CoincheckConnector, [FakeResponse(payload={"success": False})])
    with pytest.raises(ValueError, match="coincheck"):
        await conn.fetch_orderbook("BTC/JPY")


# --- quote filtering ------------------------------------------------------------


def test_jpy_connectors_only_support_jpy() -> None:
    session = as_client_session(FakeAiohttpSession())
    for cls in (BitflyerConnector, BitbankConnector, CoincheckConnector):
        conn = cls(session=session)
        assert conn.supports_quote("JPY")
        assert not conn.supports_quote("USDT")


def test_usdt_connectors_filter_jpy() -> None:
    session = as_client_session(FakeAiohttpSession())
    for cls in (OkxConnector, KucoinConnector, GateConnector, BybitConnector):
        conn = cls(session=session)
        assert conn.supports_quote("USDT")
        assert not conn.supports_quote("JPY")


def test_global_exchanges_are_usdt_only() -> None:
    session = as_client_session(FakeAiohttpSession())
    for cls in (BinanceConnector, KrakenConnector):
        conn = cls(session=session)
        assert conn.supports_quote("USDT")
        assert not conn.supports_quote("JPY")
