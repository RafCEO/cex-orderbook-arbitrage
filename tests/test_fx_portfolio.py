"""Portfolio per-quote balances, equity conversion and mixed-quote paper round trips."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.models import FeeSchedule, OrderBookSnapshot
from engine.fx import ImpliedFxCross
from execution.base import ExecutionStatus, build_intent
from execution.paper_executor import PaperExecutor
from execution.portfolio import Portfolio
from tests.fakes import make_level, make_opportunity, make_snapshot

D = Decimal

RATE = D("159.5")  # USDT per 1 JPY-unit block: 1 JPY = 1/159.5 USDT... no,
# fx_rates maps quote asset -> common (USDT) units per 1 unit of that quote.
JPY_PER_USDT = D("159.5")
USDT_PER_JPY = D(1) / JPY_PER_USDT


class TestPerQuoteBalances:
    def test_quote_assets_reported_per_exchange(self) -> None:
        p = Portfolio(
            starting_quote={"binance": D("1000"), "bitflyer": D("150000")},
            quote_assets={"binance": "USDT", "bitflyer": "JPY"},
        )
        assert p.quote_asset("binance") == "USDT"
        assert p.quote_asset("bitflyer") == "JPY"
        view = p.balances()
        assert view["binance"]["USDT"] == D("1000")
        assert view["bitflyer"]["JPY"] == D("150000")

    def test_equity_converts_jpy_holdings_to_usdt(self) -> None:
        p = Portfolio(
            starting_quote={"binance": D("1000"), "bitflyer": D("159500")},
            quote_assets={"binance": "USDT", "bitflyer": "JPY"},
        )
        # 159500 JPY * (1/159.5) = 1000 USDT
        equity = p.equity({}, {"JPY": USDT_PER_JPY})
        assert equity == pytest.approx(D("2000"), abs=D("1e-9"))

    def test_equity_without_rate_keeps_native_amount(self) -> None:
        p = Portfolio(
            starting_quote={"bitflyer": D("159500")},
            quote_assets={"bitflyer": "JPY"},
        )
        assert p.equity({}) == D("159500")

    def test_inventory_marked_in_exchange_quote_converted(self) -> None:
        p = Portfolio(
            starting_quote={"bitflyer": D("1595000")},
            quote_assets={"bitflyer": "JPY"},
        )
        # buy 1 BTC for 15950000 JPY? too much; use small amounts
        p.apply_buy("bitflyer", "BTC", D("0.01"), D("1595000"), D("0"))
        # mid on bitflyer now 16000000 JPY per BTC
        mids = {("bitflyer", "BTC"): D("16000000")}
        value = p.open_inventory_value(mids, {"JPY": USDT_PER_JPY})
        # 0.01 BTC * 16000000 JPY = 160000 JPY -> /159.5 USDT
        assert value == pytest.approx(D("160000") * USDT_PER_JPY, abs=D("1e-9"))


class TestMixedQuoteRoundTrip:
    def test_buy_jpy_sell_usdt_realized_pnl_in_usdt(self) -> None:
        """Buy BTC with JPY, sell for USDT: pnl reported in USDT via implied rate."""
        portfolio = Portfolio(
            starting_quote={"bitflyer": D("1595000"), "binance": D("1000")},
            quote_assets={"bitflyer": "JPY", "binance": "USDT"},
        )
        # buy 1 BTC for 1,550,000 JPY (fee 0): cost 1.55M JPY = 9717.87 USDT
        portfolio.apply_buy("bitflyer", "BTC", D("1"), D("1550000"), D("0"))
        portfolio.transfer("bitflyer", "binance", "BTC")
        # sell 1 BTC for 9800 USDT (fee 0)
        pnl = portfolio.apply_sell(
            "binance", "BTC", D("1"), D("9800"), D("0"), fx_rates={"JPY": USDT_PER_JPY}
        )
        # cost basis: 1550000 JPY * (1/159.5) = 9717.87...; proceeds 9800 USDT
        expected = D("9800") - D("1550000") * USDT_PER_JPY
        assert pnl == pytest.approx(expected, abs=D("1e-9"))
        assert portfolio.quote_balance("binance") == D("10800")
        assert portfolio.quote_balance("bitflyer") == D("45000")

    def test_buy_usdt_sell_jpy_leaves_jpy_inventory_quote(self) -> None:
        portfolio = Portfolio(
            starting_quote={"binance": D("10000"), "bitflyer": D("159500")},
            quote_assets={"binance": "USDT", "bitflyer": "JPY"},
        )
        portfolio.apply_buy("binance", "BTC", D("1"), D("9700"), D("0"))
        portfolio.transfer("binance", "bitflyer", "BTC")
        pnl = portfolio.apply_sell(
            "bitflyer", "BTC", D("1"), D("1560000"), D("0"), fx_rates={"JPY": USDT_PER_JPY}
        )
        # proceeds 1560000 JPY = 9780.56 USDT vs cost 9700 USDT
        expected = D("1560000") * USDT_PER_JPY - D("9700")
        assert pnl == pytest.approx(expected, abs=D("1e-9"))
        # the round trip left JPY inventory on bitflyer
        assert portfolio.quote_balance("bitflyer") == D("159500") + D("1560000")
        assert portfolio.inventory == {}

    def test_equity_sane_after_mixed_round_trip(self) -> None:
        portfolio = Portfolio(
            starting_quote={"binance": D("10000"), "bitflyer": D("159500")},
            quote_assets={"binance": "USDT", "bitflyer": "JPY"},
        )
        portfolio.apply_buy("binance", "BTC", D("1"), D("9700"), D("0"))
        portfolio.transfer("binance", "bitflyer", "BTC")
        portfolio.apply_sell(
            "bitflyer", "BTC", D("0.5"), D("780000"), D("0"), fx_rates={"JPY": USDT_PER_JPY}
        )
        mids = {("bitflyer", "BTC"): D("1560000")}
        equity = portfolio.equity(mids, {"JPY": USDT_PER_JPY})
        # cash: 300 USDT + 159500 + 780000 JPY; inventory 0.5 BTC @ 1560000 JPY
        cash_usdt = D("300") + (D("159500") + D("780000")) * USDT_PER_JPY
        inventory_usdt = D("0.5") * D("1560000") * USDT_PER_JPY
        assert equity == pytest.approx(cash_usdt + inventory_usdt, abs=D("1e-9"))


class TestPaperCrossQuoteExecution:
    def _books(self) -> dict[tuple[str, str], OrderBookSnapshot]:
        buy_book = make_snapshot(
            bids=[make_level("9690", "5")],
            asks=[make_level("9700", "5")],
            exchange="binance",
            pair="BTC/USDT",
        )
        sell_book = make_snapshot(
            bids=[make_level("1560000", "5")],
            asks=[make_level("1561000", "5")],
            exchange="bitflyer",
            pair="BTC/JPY",
        )
        return {("binance", "BTC/USDT"): buy_book, ("bitflyer", "BTC/JPY"): sell_book}

    async def test_cross_quote_intent_fills_both_legs_natively(self) -> None:
        portfolio = Portfolio(
            starting_quote={"binance": D("1000"), "bitflyer": D("0")},
            quote_assets={"binance": "USDT", "bitflyer": "JPY"},
        )
        fx = ImpliedFxCross(smoothing_window=1)
        executor = PaperExecutor(
            portfolio=portfolio,
            fees={"binance": FeeSchedule(), "bitflyer": FeeSchedule()},
            fx=fx,
        )
        books = self._books()
        executor.books = books
        opp = make_opportunity(
            buy_exchange="binance",
            sell_exchange="bitflyer",
            pair="BTC/USDT",
            volume_quote=D("485"),
            sell_pair="BTC/JPY",
            fx_quote="JPY/USDT",
            fx_rate=159.5,
        )
        intent = build_intent(
            opp,
            books[("binance", "BTC/USDT")],
            books[("bitflyer", "BTC/JPY")],
            "BTC",
            "USDT",
        )
        assert intent is not None
        report = await executor.execute(intent)
        assert report.status == ExecutionStatus.FILLED
        assert report.buy_fill is not None and report.sell_fill is not None
        # 485 USDT bought 0.05 BTC at 9700; sold for 78000 JPY at 1560000
        assert report.buy_fill.base_amount == pytest.approx(D("0.05"), abs=D("1e-12"))
        assert report.sell_fill.filled_quote == pytest.approx(D("78000"), abs=D("1e-9"))
        # portfolio now holds JPY after the sell
        assert portfolio.quote_balance("bitflyer") == pytest.approx(D("78000"), abs=D("1e-9"))
        assert portfolio.quote_balance("binance") == pytest.approx(D("515"), abs=D("1e-9"))


async def test_paper_cross_quote_report_pnl_converted() -> None:
    portfolio = Portfolio(
        starting_quote={"binance": D("1000"), "bitflyer": D("0")},
        quote_assets={"binance": "USDT", "bitflyer": "JPY"},
    )
    fx = ImpliedFxCross(smoothing_window=1)
    executor = PaperExecutor(
        portfolio=portfolio,
        fees={"binance": FeeSchedule(), "bitflyer": FeeSchedule()},
        fx=fx,
    )
    buy_book = make_snapshot(
        bids=[make_level("9690", "5")],
        asks=[make_level("9700", "5")],
        exchange="binance",
        pair="BTC/USDT",
    )
    sell_book = make_snapshot(
        bids=[make_level("1560000", "5")],
        asks=[make_level("1561000", "5")],
        exchange="bitflyer",
        pair="BTC/JPY",
    )
    executor.books = {("binance", "BTC/USDT"): buy_book, ("bitflyer", "BTC/JPY"): sell_book}
    # seed the cross so _fx_rates_for finds USDT/JPY
    fx.update({("binance", "BTC/USDT"): buy_book, ("bitflyer", "BTC/JPY"): sell_book})
    opp = make_opportunity(
        buy_exchange="binance",
        sell_exchange="bitflyer",
        pair="BTC/USDT",
        volume_quote=D("485"),
        sell_pair="BTC/JPY",
        fx_quote="JPY/USDT",
        fx_rate=159.5,
    )
    intent = build_intent(opp, buy_book, sell_book, "BTC", "USDT")
    assert intent is not None
    report = await executor.execute(intent)
    assert report.status == ExecutionStatus.FILLED
    # pnl in USDT: proceeds 78000 JPY converted at live implied rate minus cost 485
    rate = fx.rate("USDT/JPY")
    assert rate is not None
    expected = D("78000") * rate - D("485")
    assert report.realized_pnl_quote == pytest.approx(expected, abs=D("1e-9"))
    assert report.intent.fx_quote == "JPY/USDT"
