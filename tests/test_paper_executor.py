"""Paper executor: VWAP fill math, fees, balance updates, partials, round-trip P&L."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.models import FeeSchedule
from execution.base import ExecutionStatus, build_intent
from execution.paper_executor import PaperExecutor
from execution.portfolio import Portfolio
from tests.fakes import make_level, make_opportunity, make_snapshot


def make_books(
    *,
    buy_exchange: str = "exA",
    sell_exchange: str = "exB",
    bid: str = "101",
    ask: str = "100",
    bid_amount: str = "5",
    ask_amount: str = "5",
) -> tuple:
    buy_book = make_snapshot(
        bids=[make_level(bid, bid_amount)],
        asks=[make_level(ask, ask_amount)],
        exchange=buy_exchange,
    )
    sell_book = make_snapshot(
        bids=[make_level(bid, bid_amount)],
        asks=[make_level(ask, ask_amount)],
        exchange=sell_exchange,
    )
    return buy_book, sell_book


def make_executor(
    quote_a: str = "1000",
    quote_b: str = "1000",
    taker_a: str = "10",
    taker_b: str = "10",
) -> tuple[PaperExecutor, Portfolio]:
    portfolio = Portfolio(starting_quote={"exA": Decimal(quote_a), "exB": Decimal(quote_b)})
    executor = PaperExecutor(
        portfolio=portfolio,
        fees={
            "exA": FeeSchedule(taker_bps=Decimal(taker_a)),
            "exB": FeeSchedule(taker_bps=Decimal(taker_b)),
        },
    )
    return executor, portfolio


class TestFullRoundTrip:
    async def test_both_legs_walked_at_vwap_with_taker_fees(self) -> None:
        buy_book = make_snapshot(
            bids=[make_level("99", "5")], asks=[make_level("100", "5")], exchange="exA"
        )
        sell_book = make_snapshot(
            bids=[make_level("101", "5")], asks=[make_level("102", "5")], exchange="exB"
        )
        executor, portfolio = make_executor()
        executor.books = {
            ("exA", "BTC/USDT"): buy_book,
            ("exB", "BTC/USDT"): sell_book,
        }
        opp = make_opportunity(buy_exchange="exA", sell_exchange="exB", volume_quote=Decimal("500"))
        intent = build_intent(opp, buy_book, sell_book, "BTC", "USDT")
        assert intent is not None
        report = await executor.execute(intent)

        assert report.status == ExecutionStatus.FILLED
        assert report.buy_fill is not None and report.sell_fill is not None
        buy, sell = report.buy_fill, report.sell_fill
        assert buy.base_amount == Decimal("5")
        assert buy.vwap_price == Decimal("100")
        assert buy.filled_quote == Decimal("500")
        assert buy.fee_quote == Decimal("0.5")  # 10 bps on 500
        assert sell.base_amount == Decimal("5")
        assert sell.vwap_price == Decimal("101")
        assert sell.filled_quote == Decimal("505")
        assert sell.fee_quote == Decimal("0.505")
        # pnl = (505 - 0.505) - (500 + 0.5) = 3.995
        assert report.realized_pnl_quote == pytest.approx(Decimal("3.995"), abs=Decimal("1e-9"))
        assert report.simulated is True
        # exA paid 500 + 0.5 fee; exB received 505 - 0.505 fee
        assert portfolio.quote_balance("exA") == Decimal("499.5")
        assert portfolio.quote_balance("exB") == Decimal("1504.495")
        assert portfolio.inventory == {}
        assert portfolio.wins == 1

    async def test_multi_level_buy_walk_vwap(self) -> None:
        """Budget spans two ask levels: VWAP is the notional-weighted mean."""
        buy_book = make_snapshot(
            bids=[make_level("99", "5")],
            asks=[make_level("100", "1"), make_level("102", "4")],
            exchange="exA",
        )
        sell_book = make_snapshot(
            bids=[make_level("103", "10")], asks=[make_level("104", "10")], exchange="exB"
        )
        executor, _portfolio = make_executor()
        executor.books = {
            ("exA", "BTC/USDT"): buy_book,
            ("exB", "BTC/USDT"): sell_book,
        }
        opp = make_opportunity(buy_exchange="exA", sell_exchange="exB", volume_quote=Decimal("500"))
        intent = build_intent(opp, buy_book, sell_book, "BTC", "USDT")
        assert intent is not None
        report = await executor.execute(intent)
        assert report.buy_fill is not None
        # 100 quote at 100 (1 BTC) + 400 at 102 (400/102 BTC): vwap = 500/base
        expected_base = Decimal(1) + Decimal(400) / Decimal(102)
        assert report.buy_fill.base_amount == pytest.approx(expected_base, abs=Decimal("1e-12"))
        assert report.buy_fill.vwap_price == pytest.approx(
            Decimal(500) / expected_base, abs=Decimal("1e-9")
        )

    async def test_sell_leg_walks_real_bids_for_bought_base(self) -> None:
        buy_book = make_snapshot(
            bids=[make_level("99", "5")], asks=[make_level("100", "5")], exchange="exA"
        )
        sell_book = make_snapshot(
            bids=[make_level("101", "2"), make_level("100.5", "10")],
            asks=[make_level("103", "10")],
            exchange="exB",
        )
        executor, _ = make_executor()
        executor.books = {
            ("exA", "BTC/USDT"): buy_book,
            ("exB", "BTC/USDT"): sell_book,
        }
        opp = make_opportunity(buy_exchange="exA", sell_exchange="exB", volume_quote=Decimal("500"))
        intent = build_intent(opp, buy_book, sell_book, "BTC", "USDT")
        assert intent is not None
        report = await executor.execute(intent)
        assert report.sell_fill is not None
        # 2 BTC at 101 + 3 BTC at 100.5 -> filled 202 + 301.5 = 503.5
        assert report.sell_fill.filled_quote == Decimal("503.5")
        assert report.sell_fill.base_amount == Decimal("5")
        assert report.sell_fill.vwap_price == Decimal("503.5") / Decimal("5")


class TestPartialFills:
    async def test_shallow_sell_book_leaves_open_inventory(self) -> None:
        buy_book = make_snapshot(
            bids=[make_level("99", "5")], asks=[make_level("100", "5")], exchange="exA"
        )
        sell_book = make_snapshot(
            bids=[make_level("101", "2")], asks=[make_level("102", "5")], exchange="exB"
        )
        executor, portfolio = make_executor()
        executor.books = {
            ("exA", "BTC/USDT"): buy_book,
            ("exB", "BTC/USDT"): sell_book,
        }
        opp = make_opportunity(buy_exchange="exA", sell_exchange="exB", volume_quote=Decimal("500"))
        intent = build_intent(opp, buy_book, sell_book, "BTC", "USDT")
        assert intent is not None
        report = await executor.execute(intent)

        assert report.status == ExecutionStatus.PARTIAL
        assert report.sell_fill is not None
        assert report.sell_fill.base_amount == Decimal("2")
        position = portfolio.position("exB", "BTC")
        assert position.base_amount == Decimal("3")
        # 2 BTC sold at 101 = 202 gross, fee 10 bps = 0.202, net credit 201.798
        assert portfolio.quote_balance("exB") == Decimal("1000") + Decimal("201.798")

    async def test_shallow_buy_book_fills_less_than_budget(self) -> None:
        buy_book = make_snapshot(
            bids=[make_level("99", "1")], asks=[make_level("100", "1")], exchange="exA"
        )
        sell_book = make_snapshot(
            bids=[make_level("101", "10")], asks=[make_level("102", "10")], exchange="exB"
        )
        executor, _ = make_executor()
        executor.books = {
            ("exA", "BTC/USDT"): buy_book,
            ("exB", "BTC/USDT"): sell_book,
        }
        opp = make_opportunity(buy_exchange="exA", sell_exchange="exB", volume_quote=Decimal("500"))
        intent = build_intent(opp, buy_book, sell_book, "BTC", "USDT")
        assert intent is not None
        report = await executor.execute(intent)
        assert report.status == ExecutionStatus.PARTIAL
        assert report.buy_fill is not None
        assert report.buy_fill.filled_quote == Decimal("100")


class TestRejections:
    async def test_missing_book_rejected(self) -> None:
        executor, _ = make_executor()
        opp = make_opportunity(buy_exchange="exA", sell_exchange="exB")
        intent = build_intent(
            opp,
            make_snapshot(
                bids=[make_level("101", "1")], asks=[make_level("100", "1")], exchange="exA"
            ),
            make_snapshot(
                bids=[make_level("101", "1")], asks=[make_level("102", "1")], exchange="exB"
            ),
            "BTC",
            "USDT",
        )
        assert intent is not None
        report = await executor.execute(intent)  # books empty
        assert report.status == ExecutionStatus.REJECTED
        assert report.reason is not None and "missing book" in report.reason

    async def test_zero_balance_rejected(self) -> None:
        executor, _ = make_executor(quote_a="0")
        buy_book = make_snapshot(
            bids=[make_level("99", "5")], asks=[make_level("100", "5")], exchange="exA"
        )
        sell_book = make_snapshot(
            bids=[make_level("101", "5")], asks=[make_level("102", "5")], exchange="exB"
        )
        executor.books = {
            ("exA", "BTC/USDT"): buy_book,
            ("exB", "BTC/USDT"): sell_book,
        }
        opp = make_opportunity(buy_exchange="exA", sell_exchange="exB", volume_quote=Decimal("500"))
        intent = build_intent(opp, buy_book, sell_book, "BTC", "USDT")
        assert intent is not None
        report = await executor.execute(intent)
        assert report.status == ExecutionStatus.REJECTED
        assert "insufficient" in (report.reason or "")

    async def test_oversized_intent_rejected_by_balance_check(self) -> None:
        executor, portfolio = make_executor(quote_a="100")
        buy_book = make_snapshot(
            bids=[make_level("99", "50")], asks=[make_level("100", "50")], exchange="exA"
        )
        sell_book = make_snapshot(
            bids=[make_level("101", "50")], asks=[make_level("102", "50")], exchange="exB"
        )
        executor.books = {
            ("exA", "BTC/USDT"): buy_book,
            ("exB", "BTC/USDT"): sell_book,
        }
        opp = make_opportunity(
            buy_exchange="exA", sell_exchange="exB", volume_quote=Decimal("5000")
        )
        intent = build_intent(opp, buy_book, sell_book, "BTC", "USDT")
        assert intent is not None
        report = await executor.execute(intent)
        assert report.status == ExecutionStatus.REJECTED
        assert "insufficient" in (report.reason or "")
        assert portfolio.quote_balance("exA") == Decimal("100")
        assert portfolio.inventory == {}


class TestEquityMarkToMid:
    async def test_equity_after_round_trip_and_open_inventory(self) -> None:
        buy_book = make_snapshot(
            bids=[make_level("99", "5")], asks=[make_level("100", "5")], exchange="exA"
        )
        sell_book = make_snapshot(
            bids=[make_level("101", "2")], asks=[make_level("102", "5")], exchange="exB"
        )
        executor, portfolio = make_executor()
        executor.books = {
            ("exA", "BTC/USDT"): buy_book,
            ("exB", "BTC/USDT"): sell_book,
        }
        opp = make_opportunity(buy_exchange="exA", sell_exchange="exB", volume_quote=Decimal("500"))
        intent = build_intent(opp, buy_book, sell_book, "BTC", "USDT")
        assert intent is not None
        report = await executor.execute(intent)
        assert report.status == ExecutionStatus.PARTIAL
        mids = {("exB", "BTC"): Decimal("101.5")}
        # cash: exA 499.5, exB 1000 + 201.798; inventory 3 BTC marked at 101.5
        expected = Decimal("499.5") + Decimal("1201.798") + Decimal("3") * Decimal("101.5")
        assert portfolio.equity(mids) == pytest.approx(expected, abs=Decimal("1e-9"))

    async def test_report_pnl_matches_hand_computed_value(self) -> None:
        buy_book = make_snapshot(
            bids=[make_level("99", "5")], asks=[make_level("100", "5")], exchange="exA"
        )
        sell_book = make_snapshot(
            bids=[make_level("101", "5")], asks=[make_level("102", "5")], exchange="exB"
        )
        executor, _ = make_executor(taker_a="10", taker_b="26")
        executor.books = {
            ("exA", "BTC/USDT"): buy_book,
            ("exB", "BTC/USDT"): sell_book,
        }
        opp = make_opportunity(buy_exchange="exA", sell_exchange="exB", volume_quote=Decimal("500"))
        intent = build_intent(opp, buy_book, sell_book, "BTC", "USDT")
        assert intent is not None
        report = await executor.execute(intent)
        # hand: buy cost net = 500 * 1.001 = 500.5; sell proceeds net = 505 * (1 - 0.0026) = 503.687
        expected = Decimal("505") * (Decimal(1) - Decimal("26") / Decimal(10_000)) - Decimal(
            "500"
        ) * (Decimal(1) + Decimal("10") / Decimal(10_000))
        assert report.realized_pnl_quote == pytest.approx(expected, abs=Decimal("1e-9"))
        assert expected == Decimal("3.187")
