"""OrderBookSnapshot sanitization, derived properties, FeeSchedule and opportunity invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.models import (
    ArbitrageOpportunity,
    FeeSchedule,
    OrderBookLevel,
    OrderBookSnapshot,
    RouteType,
)
from tests.fakes import make_level, make_opportunity, make_snapshot

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class TestLevelSanitization:
    def test_bids_sorted_descending(self) -> None:
        snap = make_snapshot(
            bids=[make_level("100", "1"), make_level("102", "1"), make_level("101", "1")],
            asks=[make_level("103", "1")],
        )
        assert [lv.price for lv in snap.bids] == [Decimal(102), Decimal(101), Decimal(100)]

    def test_asks_sorted_ascending(self) -> None:
        snap = make_snapshot(
            bids=[make_level("100", "1")],
            asks=[make_level("105", "1"), make_level("103", "1"), make_level("104", "1")],
        )
        assert [lv.price for lv in snap.asks] == [Decimal(103), Decimal(104), Decimal(105)]

    def test_non_positive_price_levels_dropped(self) -> None:
        snap = make_snapshot(
            bids=[make_level("0", "1"), make_level("-5", "1"), make_level("100", "1")],
            asks=[make_level("0", "1"), make_level("103", "1")],
        )
        assert [lv.price for lv in snap.bids] == [Decimal(100)]
        assert [lv.price for lv in snap.asks] == [Decimal(103)]

    def test_non_positive_amount_levels_dropped(self) -> None:
        snap = make_snapshot(
            bids=[make_level("100", "0"), make_level("99", "1")],
            asks=[make_level("103", "-1"), make_level("104", "1")],
        )
        assert [lv.price for lv in snap.bids] == [Decimal(99)]
        assert [lv.price for lv in snap.asks] == [Decimal(104)]

    def test_notional_is_price_times_amount(self) -> None:
        lv = make_level("101.5", "2")
        assert lv.notional == Decimal("203.0")


class TestDerivedProperties:
    def test_mid_price_and_spread_bps(self) -> None:
        snap = make_snapshot(bids=[make_level("99", "1")], asks=[make_level("101", "1")])
        assert snap.mid_price == Decimal(100)
        assert snap.spread_bps == Decimal(200)

    def test_mid_price_none_for_empty_side(self) -> None:
        snap = make_snapshot(bids=[], asks=[make_level("101", "1")])
        assert snap.mid_price is None
        assert snap.top_bid is None
        assert snap.spread_bps is None

    def test_empty_asks_derived_none(self) -> None:
        snap = make_snapshot(bids=[make_level("99", "1")], asks=[])
        assert snap.top_ask is None
        assert snap.mid_price is None

    def test_latency_ms_from_exchange_ts(self) -> None:
        received = NOW
        snap = make_snapshot(
            bids=[make_level("99", "1")],
            asks=[make_level("101", "1")],
            received_at=received,
            latency_ms=250,
        )
        assert snap.exchange_ts == received - timedelta(milliseconds=250)
        assert snap.latency_ms == pytest.approx(250.0)

    def test_age_ms_against_wall_clock(self) -> None:
        snap = make_snapshot(
            bids=[make_level("99", "1")],
            asks=[make_level("101", "1")],
            received_at=datetime.now(UTC) - timedelta(milliseconds=1200),
        )
        assert snap.age_ms >= 1200.0

    def test_liquidity_sums_capped_at_depth(self) -> None:
        bids = [make_level(str(100 - i), "2") for i in range(15)]
        asks = [make_level(str(200 + i), "3") for i in range(15)]
        snap = make_snapshot(bids=bids, asks=asks, liquidity_depth=5)
        assert snap.bid_liquidity_quote() == sum((bids[i].notional for i in range(5)), Decimal(0))
        assert snap.ask_liquidity_quote() == sum((asks[i].notional for i in range(5)), Decimal(0))

    def test_liquidity_depth_override(self) -> None:
        bids = [make_level(str(100 - i), "1") for i in range(10)]
        snap = make_snapshot(bids=bids, asks=[], liquidity_depth=10)
        assert snap.bid_liquidity_quote(depth=2) == bids[0].notional + bids[1].notional


class TestFeeScheduleAndOpportunity:
    def test_fee_schedule_defaults_to_zero(self) -> None:
        fee = FeeSchedule()
        assert fee.maker_bps == 0
        assert fee.taker_bps == 0

    def test_is_profitable_requires_positive_net_and_full_fill(self) -> None:
        profitable = make_opportunity(net_spread_bps="12", fill_ratio="0.95")
        assert profitable.is_profitable

        negative_net = make_opportunity(net_spread_bps="-1", fill_ratio="1")
        assert not negative_net.is_profitable

        partial_fill = make_opportunity(net_spread_bps="12", fill_ratio="0.5")
        assert not partial_fill.is_profitable

    def test_fill_ratio_boundary_is_inclusive(self) -> None:
        boundary = make_opportunity(net_spread_bps="1", fill_ratio="0.9")
        assert boundary.is_profitable

    def test_opportunity_default_fields(self) -> None:
        opp = ArbitrageOpportunity(
            route=RouteType.TRIANGULAR,
            pair="BTC/USDT->ETH/BTC->ETH/USDT",
            buy_exchange="exA",
            sell_exchange="exA",
            volume_quote=Decimal(1000),
            gross_spread_bps=Decimal(1),
            slippage_bps=Decimal(1),
            fees_bps=Decimal(1),
            net_spread_bps=Decimal(1),
            net_delta_quote=Decimal(1),
            fill_ratio=Decimal(1),
            buy_latency_ms=1.0,
            sell_latency_ms=1.0,
        )
        assert not opp.suppressed
        assert (datetime.now(UTC) - opp.detected_at).total_seconds() < 60

    def test_strict_mode_rejects_float_inputs(self) -> None:
        with pytest.raises(ValueError):
            OrderBookLevel(price=1.5, amount=2.5)  # type: ignore[arg-type]  # invalid on purpose
        with pytest.raises(ValueError):
            OrderBookLevel.model_validate({"price": 1.5, "amount": 2.5})


def test_make_snapshot_factory_builds_valid_book() -> None:
    snap = make_snapshot(bids=[make_level("100", "1")], asks=[make_level("102", "1")])
    assert isinstance(snap, OrderBookSnapshot)
    assert snap.pair == "BTC/USDT"
    assert isinstance(snap.bids[0], OrderBookLevel)
