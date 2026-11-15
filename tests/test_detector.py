"""SpreadDetector: pairing, staleness, spike filter integration, latency propagation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.models import FeeSchedule, OrderBookSnapshot, RouteType
from engine.detector import SpikeFilterState, SpreadDetector
from tests.fakes import make_level, make_snapshot


def fresh_snapshot(
    exchange: str,
    *,
    pair: str = "BTC/USDT",
    bid: str = "100",
    ask: str = "101",
    latency_ms: float = 0.0,
    amount: str = "10",
    age_ms: float = 0.0,
) -> OrderBookSnapshot:
    return make_snapshot(
        bids=[make_level(bid, amount)],
        asks=[make_level(ask, amount)],
        exchange=exchange,
        pair=pair,
        latency_ms=latency_ms,
        age_ms=age_ms,
    )


def make_detector(**kwargs: object) -> SpreadDetector:
    defaults: dict[str, object] = {
        "min_spread_bps": 5.0,
        "max_snapshot_age_ms": 5000.0,
        "spike_window": 20,
        "spike_deviation_bps": 25.0,
        "confirmations": 2,
    }
    defaults.update(kwargs)
    # ignore: test helper accepts loose kwargs, SpreadDetector validates them
    return SpreadDetector(**defaults)  # type: ignore[arg-type]


ZERO_FEES = {"exA": FeeSchedule(), "exB": FeeSchedule(), "exC": FeeSchedule()}


class TestCrossExchangePairing:
    def test_three_exchanges_six_directions(self) -> None:
        snaps = {
            ("exA", "BTC/USDT"): fresh_snapshot("exA", bid="99", ask="100"),
            ("exB", "BTC/USDT"): fresh_snapshot("exB", bid="101", ask="102"),
            ("exC", "BTC/USDT"): fresh_snapshot("exC", bid="103", ask="104"),
        }
        det = make_detector()
        det.evaluate_tick(snaps, Decimal(1000), ZERO_FEES)  # seed spike filter
        opps = det.evaluate_tick(snaps, Decimal(1000), ZERO_FEES)
        routes = {(o.buy_exchange, o.sell_exchange) for o in opps}
        assert routes == {
            ("exA", "exB"),
            ("exA", "exC"),
            ("exB", "exA"),
            ("exB", "exC"),
            ("exC", "exA"),
            ("exC", "exB"),
        }
        assert all(o.route == RouteType.CROSS_EXCHANGE for o in opps)
        assert all(o.pair == "BTC/USDT" for o in opps)

    def test_best_route_direction_has_positive_net(self) -> None:
        snaps = {
            ("exA", "BTC/USDT"): fresh_snapshot("exA", bid="99", ask="100"),
            ("exB", "BTC/USDT"): fresh_snapshot("exB", bid="101", ask="102"),
        }
        det = make_detector()
        det.evaluate_tick(snaps, Decimal(1000), ZERO_FEES)
        opps = det.evaluate_tick(snaps, Decimal(1000), ZERO_FEES)
        exa_buy = next(o for o in opps if o.buy_exchange == "exA" and not o.suppressed)
        assert float(exa_buy.gross_spread_bps) > 0
        assert exa_buy.is_profitable
        exb_buy = next(o for o in opps if o.buy_exchange == "exB" and not o.suppressed)
        assert float(exb_buy.gross_spread_bps) < 0

    def test_single_exchange_yields_no_cross_routes(self) -> None:
        snaps = {("exA", "BTC/USDT"): fresh_snapshot("exA")}
        assert make_detector().evaluate_tick(snaps, Decimal(1000), ZERO_FEES) == []

    def test_one_empty_book_side_yields_no_opportunity(self) -> None:
        snaps = {
            ("exA", "BTC/USDT"): make_snapshot(bids=[], asks=[]),
            ("exB", "BTC/USDT"): fresh_snapshot("exB"),
        }
        assert make_detector().evaluate_tick(snaps, Decimal(1000), ZERO_FEES) == []


class TestStalenessGuard:
    def test_stale_snapshot_excluded(self) -> None:
        snaps = {
            ("exA", "BTC/USDT"): fresh_snapshot("exA", age_ms=6000),
            ("exB", "BTC/USDT"): fresh_snapshot("exB"),
        }
        assert make_detector().evaluate_tick(snaps, Decimal(1000), ZERO_FEES) == []

    def test_fresh_boundary_included(self) -> None:
        snaps = {
            ("exA", "BTC/USDT"): fresh_snapshot("exA", age_ms=1000),
            ("exB", "BTC/USDT"): fresh_snapshot("exB", age_ms=1000),
        }
        opps = make_detector().evaluate_tick(snaps, Decimal(1000), ZERO_FEES)
        assert len(opps) == 2


class TestLatencyPropagation:
    def test_latencies_flow_into_opportunity(self) -> None:
        snaps = {
            ("exA", "BTC/USDT"): fresh_snapshot("exA", bid="99", ask="100", latency_ms=120.0),
            ("exB", "BTC/USDT"): fresh_snapshot("exB", bid="101", ask="102", latency_ms=340.0),
        }
        det = make_detector()
        det.evaluate_tick(snaps, Decimal(1000), ZERO_FEES)
        opps = det.evaluate_tick(snaps, Decimal(1000), ZERO_FEES)
        exa_buy = next(o for o in opps if o.buy_exchange == "exA" and not o.suppressed)
        assert exa_buy.buy_latency_ms == pytest.approx(120.0, abs=5.0)
        assert exa_buy.sell_latency_ms == pytest.approx(340.0, abs=5.0)


class TestSpikeFilterThroughDetector:
    def _market(self, exb_bid: str) -> dict[tuple[str, str], OrderBookSnapshot]:
        return {
            ("exA", "BTC/USDT"): fresh_snapshot("exA", bid="99", ask="100"),
            ("exB", "BTC/USDT"): fresh_snapshot("exB", bid=exb_bid, ask="103.1"),
        }

    def test_outlier_suppressed_flagged(self) -> None:
        det = make_detector()
        fees = {"exA": FeeSchedule(), "exB": FeeSchedule()}
        for _ in range(5):
            det.evaluate_tick(self._market("100.02"), Decimal(1000), fees)
        opps = det.evaluate_tick(self._market("103"), Decimal(1000), fees)
        spiked = [o for o in opps if o.buy_exchange == "exA" and o.sell_exchange == "exB"]
        assert spiked and all(o.suppressed for o in spiked)

    def test_persistent_shift_passes_after_confirmations(self) -> None:
        det = make_detector()
        fees = {"exA": FeeSchedule(), "exB": FeeSchedule()}
        for _ in range(5):
            det.evaluate_tick(self._market("100.02"), Decimal(1000), fees)
        first = det.evaluate_tick(self._market("103"), Decimal(1000), fees)
        spiked = [o for o in first if o.buy_exchange == "exA" and o.sell_exchange == "exB"]
        assert spiked and all(o.suppressed for o in spiked)
        second = det.evaluate_tick(self._market("103"), Decimal(1000), fees)
        confirmed = [o for o in second if o.buy_exchange == "exA" and o.sell_exchange == "exB"]
        assert confirmed and all(not o.suppressed for o in confirmed)


class TestSpikeFilterStateUnit:
    def test_median_even_count_averages_middle_pair(self) -> None:
        f = SpikeFilterState(window=10, deviation_bps=1000.0, confirmations=99)
        for v in (10, 20, 30, 40):
            f.accept(Decimal(v))
        assert f.accept(Decimal(25)) is True
        assert f._median() == Decimal(25)

    def test_rolling_window_evicts_oldest(self) -> None:
        f = SpikeFilterState(window=3, deviation_bps=1.0, confirmations=99)
        for _ in range(3):
            f.accept(Decimal(0))
        for _ in range(3):
            f.accept(Decimal(100))  # outliers rejected, not recorded
        assert len(f.readings) == 3
        # deviation window still median 0; 0 passes and stays consistent
        assert f.accept(Decimal(0)) is True
        assert f.accept(Decimal(1)) is True  # deviation 1 not > 1

    def test_streak_resets_on_below_min_reading(self) -> None:
        f = SpikeFilterState(window=5, deviation_bps=1000.0, confirmations=2, min_spread_bps=5.0)
        assert f.accept(Decimal(10)) is True
        assert f.accept(Decimal(10)) is True
        assert f.accept(Decimal(0)) is True
        assert f.streak == 0

    def test_deviation_boundary_inclusive(self) -> None:
        f = SpikeFilterState(window=10, deviation_bps=25.0, confirmations=99, min_spread_bps=100.0)
        for _ in range(5):
            f.accept(Decimal(0))
        assert f.accept(Decimal(25)) is True


TRIPLET: list[tuple[str, str, str]] = [("BTC/USDT", "ETH/BTC", "ETH/USDT")]


class TestTripletRouting:
    def _snaps(self) -> dict[tuple[str, str], OrderBookSnapshot]:
        # implied = bid_c / (ask_a * ask_b) = 5000 / (100000 * 0.05) = 1.0
        return {
            ("exA", "BTC/USDT"): fresh_snapshot("exA", pair="BTC/USDT", bid="99900", ask="100000"),
            ("exA", "ETH/BTC"): fresh_snapshot("exA", pair="ETH/BTC", bid="0.049", ask="0.05"),
            ("exA", "ETH/USDT"): fresh_snapshot("exA", pair="ETH/USDT", bid="5000", ask="5010"),
            ("exB", "BTC/USDT"): fresh_snapshot("exB"),
        }

    def test_triangular_route_emitted(self) -> None:
        det = make_detector()
        snaps = self._snaps()
        det.evaluate_tick(snaps, Decimal(1000), ZERO_FEES, TRIPLET)
        opps = det.evaluate_tick(snaps, Decimal(1000), ZERO_FEES, TRIPLET)
        tri = [o for o in opps if o.route == RouteType.TRIANGULAR]
        assert len(tri) == 1
        assert tri[0].buy_exchange == "exA" == tri[0].sell_exchange
        assert tri[0].pair == "BTC/USDT->ETH/BTC->ETH/USDT"

    def test_incomplete_triplet_skipped(self) -> None:
        snaps = self._snaps()
        del snaps[("exA", "ETH/USDT")]
        det = make_detector()
        det.evaluate_tick(snaps, Decimal(1000), ZERO_FEES, TRIPLET)
        opps = det.evaluate_tick(snaps, Decimal(1000), ZERO_FEES, TRIPLET)
        assert [o for o in opps if o.route == RouteType.TRIANGULAR] == []


def test_min_spread_threshold_splits_emission() -> None:
    # With confirmations=1 the route confirms on the first above-min reading.
    det = make_detector(confirmations=1)
    snaps = {
        ("exA", "BTC/USDT"): fresh_snapshot("exA", bid="99", ask="100"),
        ("exB", "BTC/USDT"): fresh_snapshot("exB", bid="101", ask="102"),
    }
    fees = {"exA": FeeSchedule(), "exB": FeeSchedule()}
    opps = det.evaluate_tick(snaps, Decimal(1000), fees)
    profitable = [o for o in opps if o.buy_exchange == "exA"]
    assert profitable and all(not o.suppressed for o in profitable)
