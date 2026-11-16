"""ImpliedFxCross: rate derivation, smoothing, staleness, sanity band, conversion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.models import OrderBookSnapshot
from engine.detector import SpreadDetector
from engine.fx import ImpliedFxCross, quote_of
from engine.vwap_calculator import evaluate_cross_quote
from tests.fakes import make_level, make_snapshot

D = Decimal

JPY_BID = D("10266000")
JPY_MID = (JPY_BID + JPY_BID + 100) / 2
USDT_BID = D("64400")
USDT_MID = (USDT_BID + USDT_BID + 10) / 2


def fx_market(
    jpy_mid: str = "10266000",
    usdt_mid: str = "64400",
    *,
    age_ms: float = 0.0,
    exchanges: tuple[str, str] = ("bitflyer", "binance"),
) -> dict[tuple[str, str], OrderBookSnapshot]:
    jpy_ex, usdt_ex = exchanges
    return {
        (jpy_ex, "BTC/JPY"): make_snapshot(
            bids=[make_level(jpy_mid, "1")],
            asks=[make_level(str(D(jpy_mid) + 100), "1")],
            exchange=jpy_ex,
            pair="BTC/JPY",
            age_ms=age_ms,
        ),
        (usdt_ex, "BTC/USDT"): make_snapshot(
            bids=[make_level(usdt_mid, "1")],
            asks=[make_level(str(D(usdt_mid) + 10), "1")],
            exchange=usdt_ex,
            pair="BTC/USDT",
            age_ms=age_ms,
        ),
    }


class TestImpliedRate:
    def test_rate_from_two_mids(self) -> None:
        fx = ImpliedFxCross(smoothing_window=1)
        fx.update(fx_market())
        expected = JPY_MID / USDT_MID
        assert fx.rate("JPY/USDT") == pytest.approx(expected, rel=D("1e-9"))
        assert fx.rate("USDT/JPY") == pytest.approx(D(1) / expected, rel=D("1e-9"))

    def test_uses_freshest_snapshot_per_quote(self) -> None:
        fx = ImpliedFxCross(smoothing_window=1)
        older = make_snapshot(
            bids=[make_level("99900000", "1")],
            asks=[make_level("99900100", "1")],
            exchange="bitbank",
            pair="BTC/JPY",
            received_at=datetime.now(UTC) - timedelta(seconds=2),
        )
        newer = make_snapshot(
            bids=[make_level(JPY_BID, "1")],
            asks=[make_level(str(JPY_BID + 100), "1")],
            exchange="bitflyer",
            pair="BTC/JPY",
        )
        snaps = {
            ("bitbank", "BTC/JPY"): older,
            ("bitflyer", "BTC/JPY"): newer,
            ("binance", "BTC/USDT"): make_snapshot(
                bids=[make_level(USDT_BID, "1")],
                asks=[make_level(str(USDT_BID + 10), "1")],
                exchange="binance",
                pair="BTC/USDT",
            ),
        }
        fx.update(snaps)
        assert fx.rate("JPY/USDT") == pytest.approx(JPY_MID / USDT_MID, rel=D("1e-9"))

    def test_stale_input_ignored(self) -> None:
        fx = ImpliedFxCross(smoothing_window=1)
        fx.update(fx_market(age_ms=6000))
        assert fx.rate("JPY/USDT") is None

    def test_ignores_non_reference_bases(self) -> None:
        fx = ImpliedFxCross(reference_base="BTC", smoothing_window=1)
        snaps = {
            ("binance", "ETH/USDT"): make_snapshot(
                bids=[make_level(USDT_MID, "1")],
                asks=[make_level("64410", "1")],
                exchange="binance",
                pair="ETH/USDT",
            ),
            ("binance", "BTC/JPY"): make_snapshot(
                bids=[make_level(JPY_MID, "1")],
                asks=[make_level("10266100", "1")],
                exchange="binance",
                pair="BTC/JPY",
            ),
        }
        fx.update(snaps)
        assert fx.rate("JPY/USDT") is None  # no BTC/USDT book: nothing to divide

    def test_convert_amounts_between_quotes(self) -> None:
        fx = ImpliedFxCross(smoothing_window=1)
        fx.update(fx_market())
        rate = fx.rate("JPY/USDT")
        assert rate is not None
        converted = fx.convert(D("1000"), "JPY", "USDT")
        assert converted == pytest.approx(D("1000") / rate, rel=D("1e-9"))
        assert fx.convert(D("5"), "USDT", "USDT") == D("5")


class TestRollingMedianSmoothing:
    def test_median_over_window(self) -> None:
        fx = ImpliedFxCross(smoothing_window=3)
        for jpy in ("10266000", "10266000", "10300000"):
            fx.update(fx_market(jpy_mid=jpy))
        # window holds 10266000, 10266000, 10300000 -> median 10266000
        assert fx.rate("JPY/USDT") == pytest.approx(JPY_MID / USDT_MID, rel=D("1e-9"))

    def test_window_evicts_oldest(self) -> None:
        fx = ImpliedFxCross(smoothing_window=2)
        for jpy in ("10266000", "10400000", "10400000"):
            fx.update(fx_market(jpy_mid=jpy))
        # window holds only the two 10400000-bid readings, each with mid 10400050
        assert fx.rate("JPY/USDT") == pytest.approx(D("10400050") / USDT_MID, rel=D("1e-9"))

    def test_single_thin_print_does_not_whipsaw(self) -> None:
        fx = ImpliedFxCross(smoothing_window=5)
        for _ in range(4):
            fx.update(fx_market())
        steady = fx.rate("JPY/USDT")
        fx.update(fx_market(jpy_mid="11000000"))  # one thin print
        assert fx.rate("JPY/USDT") == pytest.approx(steady, rel=D("1e-9"))


class TestSanityBand:
    def test_degrades_on_excess_deviation(self) -> None:
        fx = ImpliedFxCross(smoothing_window=5, sanity_band_bps=200.0)
        for _ in range(3):
            fx.update(fx_market())
        fx.update(fx_market(jpy_mid="12000000"))  # >200bps off the median
        state = fx.state("JPY/USDT")
        assert state is not None and state.degraded
        assert fx.usable("JPY/USDT") is False
        assert state.rate is not None  # last good median retained

    def test_recover_after_degraded(self) -> None:
        fx = ImpliedFxCross(smoothing_window=5, sanity_band_bps=200.0)
        for _ in range(3):
            fx.update(fx_market())
        fx.update(fx_market(jpy_mid="12000000"))
        assert not fx.usable("JPY/USDT")
        fx.update(fx_market())  # back inside the band
        assert fx.usable("JPY/USDT")

    def test_within_band_rate_keeps_updating(self) -> None:
        fx = ImpliedFxCross(smoothing_window=5, sanity_band_bps=200.0)
        for _ in range(3):
            fx.update(fx_market())
        fx.update(fx_market(jpy_mid="10280000"))  # ~20bps drift, allowed
        state = fx.state("JPY/USDT")
        assert state is not None and not state.degraded
        assert fx.usable("JPY/USDT")


class TestStalenessGuard:
    def test_usable_fresh_then_stale_by_age(self) -> None:
        fx = ImpliedFxCross(smoothing_window=1, max_age_ms=5000.0)
        fx.update(fx_market())
        assert fx.usable("JPY/USDT")
        future = datetime.now(UTC) + timedelta(seconds=6)
        assert not fx.usable("JPY/USDT", now=future)

    def test_cross_never_updated_is_not_usable(self) -> None:
        fx = ImpliedFxCross()
        assert not fx.usable("JPY/USDT")


class TestHaircut:
    def test_haircut_mult_below_one(self) -> None:
        fx = ImpliedFxCross(conversion_haircut_bps=25.0)
        assert fx.haircut_mult() == pytest.approx(D("0.9975"), abs=D("1e-9"))

    def test_haircut_zero_is_identity(self) -> None:
        fx = ImpliedFxCross(conversion_haircut_bps=0.0)
        assert fx.haircut_mult() == 1


class TestEvaluateCrossQuote:
    def _books(
        self, jpy_bid: str = "10266000", usdt_ask: str = "64400"
    ) -> tuple[OrderBookSnapshot, OrderBookSnapshot]:
        buy = make_snapshot(
            bids=[make_level("10265000", "5")],
            asks=[make_level(usdt_ask, "5")],
            exchange="binance",
            pair="BTC/USDT",
        )
        sell = make_snapshot(
            bids=[make_level(jpy_bid, "5")],
            asks=[make_level(str(D(jpy_bid) + 100), "5")],
            exchange="bitflyer",
            pair="BTC/JPY",
        )
        return buy, sell

    def test_jpy_sell_normalized_into_usdt_book(self) -> None:
        buy, sell = self._books(jpy_bid="10266000", usdt_ask="64400")
        # USDT/JPY: common-quote units (USDT) per foreign unit (JPY)
        rate = USDT_MID / JPY_MID
        opp = evaluate_cross_quote(buy, sell, D("64000"), _zero_fees(), _zero_fees(), rate, D("0"))
        assert opp is not None
        assert opp.fx_quote == "JPY/USDT"
        assert opp.sell_pair == "BTC/JPY"
        # sell bid 10266000 JPY * rate ~= 64378 USDT vs buy ask 64400 USDT
        normalized_bid = D("10266000") * rate
        expected_gross = (normalized_bid - D("64400")) / D("64400") * D(10_000)
        assert opp.gross_spread_bps == pytest.approx(expected_gross, abs=D("0.5"))
        assert opp.fx_rate == pytest.approx(float(JPY_MID / USDT_MID), rel=1e-9)

    def test_haircut_applied_exactly_once(self) -> None:
        buy, sell = self._books()
        rate = USDT_MID / JPY_MID
        clean = evaluate_cross_quote(
            buy, sell, D("64000"), _zero_fees(), _zero_fees(), rate, D("0")
        )
        shaved = evaluate_cross_quote(
            buy, sell, D("64000"), _zero_fees(), _zero_fees(), rate, D("25")
        )
        assert clean is not None and shaved is not None
        # 25 bps haircut on the sell side only: net drops by ~25 bps (rate
        # scaling keeps the buy reference fixed; deviation is second order)
        assert clean.net_spread_bps - shaved.net_spread_bps == pytest.approx(D("25"), abs=D("0.01"))

    def test_non_common_quotes_rejected(self) -> None:
        a = make_snapshot(
            bids=[make_level("100", "1")],
            asks=[make_level("101", "1")],
            exchange="exA",
            pair="BTC/JPY",
        )
        b = make_snapshot(
            bids=[make_level("100", "1")],
            asks=[make_level("101", "1")],
            exchange="exB",
            pair="BTC/EUR",
        )
        assert (
            evaluate_cross_quote(a, b, D("1000"), _zero_fees(), _zero_fees(), D("1"), D("0"))
            is None
        )

    def test_zero_rate_rejected(self) -> None:
        buy, sell = self._books()
        assert (
            evaluate_cross_quote(buy, sell, D("64000"), _zero_fees(), _zero_fees(), D("0"), D("0"))
            is None
        )


def _zero_fees():
    from core.models import FeeSchedule

    return FeeSchedule()


class TestDetectorCrossQuoteRouting:
    def _snaps(self) -> dict[tuple[str, str], OrderBookSnapshot]:
        return fx_market(jpy_mid="10266000", usdt_mid="64400")

    def _detector(self, fx: ImpliedFxCross) -> SpreadDetector:
        return SpreadDetector(
            min_spread_bps=5.0,
            max_snapshot_age_ms=5000.0,
            spike_window=20,
            spike_deviation_bps=25.0,
            confirmations=2,
            fx=fx,
        )

    def test_cross_quote_route_emitted_with_fx_fields(self) -> None:
        fx = ImpliedFxCross(smoothing_window=1)
        fx.update(self._snaps())
        det = self._detector(fx)
        fees = {"binance": _zero_fees(), "bitflyer": _zero_fees()}
        snaps = self._snaps()
        det.evaluate_tick(snaps, D("1000"), fees)  # seed filter
        opps = det.evaluate_tick(snaps, D("1000"), fees)
        cross = [o for o in opps if o.fx_quote and not o.suppressed]
        assert cross, "expected at least one emitted cross-quote route"
        for opp in cross:
            assert opp.fx_quote == "JPY/USDT"
            assert opp.fx_rate > 100  # JPY per USDT, sanity scale
            assert opp.sell_pair == "BTC/JPY" or opp.pair == "BTC/JPY"

    def test_cross_quote_suppressed_when_degraded(self) -> None:
        fx = ImpliedFxCross(smoothing_window=5, sanity_band_bps=200.0)
        for _ in range(3):
            fx.update(self._snaps())
        degraded_market = fx_market(jpy_mid="12000000")
        fx.update(degraded_market)
        det = self._detector(fx)
        fees = {"binance": _zero_fees(), "bitflyer": _zero_fees()}
        opps = det.evaluate_tick(degraded_market, D("1000"), fees)
        assert [o for o in opps if o.fx_quote] == []

    def test_no_fx_means_no_cross_quote_routes(self) -> None:
        det = self._detector(ImpliedFxCross(smoothing_window=1))
        fees = {"binance": _zero_fees(), "bitflyer": _zero_fees()}
        snaps = self._snaps()
        det.evaluate_tick(snaps, D("1000"), fees)
        opps = det.evaluate_tick(snaps, D("1000"), fees)
        assert [o for o in opps if o.fx_quote] == []

    def test_same_quote_routes_unchanged_by_fx_presence(self) -> None:
        """Regression: identical same-quote emissions with and without fx attached."""
        snaps = {
            ("exA", "BTC/USDT"): make_snapshot(
                bids=[make_level("99", "5")],
                asks=[make_level("100", "5")],
                exchange="exA",
                pair="BTC/USDT",
            ),
            ("exB", "BTC/USDT"): make_snapshot(
                bids=[make_level("101", "5")],
                asks=[make_level("102", "5")],
                exchange="exB",
                pair="BTC/USDT",
            ),
        }
        fees = {"exA": _zero_fees(), "exB": _zero_fees()}

        def run(det: SpreadDetector) -> list[tuple[str, str, str, str, str]]:
            det.evaluate_tick(snaps, D("1000"), fees)
            return [
                (
                    o.pair,
                    o.buy_exchange,
                    o.sell_exchange,
                    str(o.net_spread_bps.quantize(D("0.000001"))),
                    str(o.suppressed),
                )
                for o in det.evaluate_tick(snaps, D("1000"), fees)
            ]

        without = run(SpreadDetector())
        with_fx = run(self._detector(ImpliedFxCross(smoothing_window=1)))
        assert without == with_fx
        assert len(without) == 2


def test_quote_of_extracts_suffix() -> None:
    assert quote_of("BTC/USDT") == "USDT"
    assert quote_of("ETH/JPY") == "JPY"
