"""Hermetic unit tests: VWAP walk, slippage, fee math, spike filter, latency guard, symbols."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from connectors.kraken import KrakenConnector
from core.models import FeeSchedule, OrderBookLevel, OrderBookSnapshot
from engine.detector import SpikeFilterState, SpreadDetector
from engine.vwap_calculator import evaluate_cross_exchange, slippage_bps, walk_depth

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def level(price: str, amount: str) -> OrderBookLevel:
    return OrderBookLevel(price=Decimal(price), amount=Decimal(amount))


def snapshot(
    bids: list[OrderBookLevel],
    asks: list[OrderBookLevel],
    exchange: str = "exA",
    pair: str = "BTC/USDT",
    latency_ms: int = 0,
    received_at: datetime | None = None,
) -> OrderBookSnapshot:
    received = received_at or NOW
    return OrderBookSnapshot(
        exchange=exchange,
        pair=pair,
        bids=bids,
        asks=asks,
        exchange_ts=received - timedelta(milliseconds=latency_ms),
        received_at=received,
    )


def test_exact_vwap_three_levels() -> None:
    asks = [level("100", "2"), level("110", "2"), level("120", "2")]
    res = walk_depth(asks, Decimal(600))
    assert res.filled_quote == pytest.approx(Decimal(600))
    assert res.base_filled == pytest.approx(Decimal("5.5"))
    assert res.vwap_price == pytest.approx(Decimal(600) / Decimal("5.5"))
    assert res.levels_consumed == 3
    assert res.fill_ratio == Decimal(1)
    assert res.residual_quote == 0


def test_slippage_bps_vs_top_of_book() -> None:
    assert slippage_bps(Decimal(105), Decimal(100)) == Decimal(500)
    assert slippage_bps(Decimal(99.5), Decimal(100)) == Decimal(-50)
    assert slippage_bps(Decimal(105), Decimal(0)) == 0


def test_partial_fill_shallow_book() -> None:
    asks = [level("100", "1")]
    res = walk_depth(asks, Decimal(500))
    assert res.fill_ratio == pytest.approx(Decimal("0.2"))
    assert res.vwap_price == Decimal(100)
    assert res.levels_consumed == 1
    assert res.residual_quote == Decimal(400)


def test_net_spread_after_fees_both_legs() -> None:
    buy = snapshot([level("99", "10")], [level("100", "10")], exchange="exA")
    sell = snapshot([level("101", "10")], [level("102", "10")], exchange="exB")
    fee = FeeSchedule(taker_bps=Decimal(10), maker_bps=Decimal(10))
    opp = evaluate_cross_exchange(buy, sell, Decimal(1000), fee, fee)
    assert opp is not None
    assert float(opp.gross_spread_bps) == pytest.approx(100.0, abs=1e-9)
    assert float(opp.fees_bps) == pytest.approx(20.0, abs=1e-9)
    assert float(opp.slippage_bps) == pytest.approx(0.0, abs=1e-9)
    assert float(opp.net_spread_bps) == pytest.approx(80.0, abs=1e-9)
    assert float(opp.net_delta_quote) == pytest.approx(
        1010 * (1 - 0.001) - 1000 * (1 + 0.001), abs=1e-9
    )
    assert opp.is_profitable


def test_profitable_and_non_profitable_routes() -> None:
    fee = FeeSchedule(taker_bps=Decimal(10), maker_bps=Decimal(10))
    skewed_buy = snapshot([level("100", "10")], [level("100", "10")], exchange="exA")
    skewed_sell = snapshot([level("102", "10")], [level("103", "10")], exchange="exB")
    opp = evaluate_cross_exchange(skewed_buy, skewed_sell, Decimal(1000), fee, fee)
    assert opp is not None and opp.is_profitable

    tight_buy = snapshot([level("100", "10")], [level("100.1", "10")], exchange="exA")
    tight_sell = snapshot([level("100.12", "10")], [level("100.2", "10")], exchange="exB")
    opp2 = evaluate_cross_exchange(tight_buy, tight_sell, Decimal(1000), fee, fee)
    assert opp2 is not None and not opp2.is_profitable


def test_spike_filter_single_outlier_suppressed() -> None:
    f = SpikeFilterState(window=20, deviation_bps=25.0, confirmations=2, min_spread_bps=5.0)
    for _ in range(5):
        assert f.accept(Decimal(0))
    assert not f.accept(Decimal(60))
    assert len(f.readings) == 5


def test_spike_filter_persistent_shift_confirms() -> None:
    f = SpikeFilterState(window=20, deviation_bps=25.0, confirmations=2, min_spread_bps=5.0)
    for _ in range(5):
        f.accept(Decimal(0))
    first = f.accept(Decimal(40))
    second = f.accept(Decimal(40))
    third = f.accept(Decimal(44))
    assert first is False
    assert second is True
    assert third is True


def test_stale_snapshot_rejected_by_latency_guard() -> None:
    stale = snapshot(
        [level("100", "5")],
        [level("101", "5")],
        exchange="exA",
        received_at=datetime.now(timezone.utc) - timedelta(seconds=30),
    )
    fresh = snapshot(
        [level("100", "5")],
        [level("101", "5")],
        exchange="exB",
        received_at=datetime.now(timezone.utc),
    )
    detector = SpreadDetector(min_spread_bps=5.0, max_snapshot_age_ms=5000.0)
    opps = detector.evaluate_tick(
        {("exA", "BTC/USDT"): stale, ("exB", "BTC/USDT"): fresh},
        Decimal(1000),
        {"exA": FeeSchedule(), "exB": FeeSchedule()},
    )
    assert opps == []


def test_kraken_symbol_normalization() -> None:
    assert KrakenConnector.to_exchange_symbol("BTC/USDT") == "XBTUSDT"
    assert KrakenConnector.to_canonical_pair("XBTUSDT") == "BTC/USDT"
    assert KrakenConnector.to_canonical_pair("XXBTZUSDT") == "BTC/USDT"
    assert KrakenConnector.to_exchange_symbol("ETH/USDT") == "ETHUSDT"
    assert KrakenConnector.to_canonical_pair("ETHUSDT") == "ETH/USDT"


def test_snapshot_orders_and_filters_levels() -> None:
    bids = [level("100", "1"), level("102", "1"), level("101", "1"), level("50", "0")]
    asks = [level("104", "1"), level("103", "1"), level("105", "-1")]
    snap = snapshot(bids, asks)
    assert [str(b.price) for b in snap.bids] == ["102", "101", "100"]
    assert [str(a.price) for a in snap.asks] == ["103", "104"]
    assert snap.top_bid == Decimal(102)
    assert snap.top_ask == Decimal(103)
    assert snap.mid_price == Decimal("102.5")
    assert snap.spread_bps == pytest.approx(Decimal(10000) / Decimal("102.5"))


def test_detector_emits_cross_exchange_opportunity() -> None:
    fresh = datetime.now(timezone.utc)
    buy = snapshot([level("99", "10")], [level("100", "10")], exchange="exA", received_at=fresh)
    sell = snapshot([level("101", "10")], [level("102", "10")], exchange="exB", received_at=fresh)
    detector = SpreadDetector(min_spread_bps=5.0, max_snapshot_age_ms=5000.0)
    opps = detector.evaluate_tick(
        {("exA", "BTC/USDT"): buy, ("exB", "BTC/USDT"): sell},
        Decimal(1000),
        {"exA": FeeSchedule(taker_bps=Decimal(10)), "exB": FeeSchedule(taker_bps=Decimal(10))},
    )
    assert len(opps) == 2
    exa_to_exb = [o for o in opps if o.buy_exchange == "exA" and not o.suppressed]
    assert len(exa_to_exb) == 1
    assert float(exa_to_exb[0].net_spread_bps) == pytest.approx(80.0, abs=1e-9)
