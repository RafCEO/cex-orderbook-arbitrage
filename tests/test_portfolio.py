"""Portfolio: balance invariants, round-trip realized P&L, equity mark-to-mid."""

from __future__ import annotations

from decimal import Decimal

import pytest

from execution.portfolio import Portfolio

D = Decimal


class TestBalanceInvariants:
    def test_buy_debits_quote_and_opens_position(self) -> None:
        p = Portfolio(starting_quote={"exA": D("1000")})
        p.apply_buy("exA", "BTC", D("2"), D("300"), D("0.3"))
        assert p.quote_balance("exA") == D("699.7")
        pos = p.position("exA", "BTC")
        assert pos.base_amount == D("2")
        assert pos.cost_quote == D("300.3")  # all-in cost basis
        assert pos.avg_cost == D("150.15")

    def test_sell_credits_quote_and_closes_position(self) -> None:
        p = Portfolio(starting_quote={"exA": D("1000")})
        p.apply_buy("exA", "BTC", D("2"), D("300"), D("0"))
        pnl = p.apply_sell("exA", "BTC", D("2"), D("320"), D("0"))
        assert p.quote_balance("exA") == D("1020")
        assert pnl == D("20")
        assert p.realized_pnl_quote == D("20")
        assert ("exA", "BTC") not in p.inventory

    def test_unknown_exchange_sell_creates_credit_balance(self) -> None:
        p = Portfolio()
        pnl = p.apply_sell("exB", "BTC", D("1"), D("100"), D("0.1"))
        assert p.quote_balance("exB") == D("99.9")
        assert pnl == D("0")

    def test_buy_then_partial_sell_leaves_remainder(self) -> None:
        p = Portfolio(starting_quote={"exA": D("1000")})
        p.apply_buy("exA", "BTC", D("4"), D("400"), D("0"))
        pnl = p.apply_sell("exA", "BTC", D("1"), D("110"), D("0"))
        assert pnl == D("10")
        pos = p.position("exA", "BTC")
        assert pos.base_amount == D("3")
        assert pos.cost_quote == D("300")

    def test_sell_more_than_inventory_closes_all(self) -> None:
        p = Portfolio(starting_quote={"exA": D("1000")})
        p.apply_buy("exA", "BTC", D("1"), D("100"), D("0"))
        p.apply_sell("exA", "BTC", D("5"), D("600"), D("0"))
        assert ("exA", "BTC") not in p.inventory
        assert p.position("exA", "BTC").base_amount == 0


class TestRoundTripRealizedPnl:
    def test_full_round_trip_matches_hand_computation(self) -> None:
        """Buy 5 BTC for 500 + 0.5 fee; sell all for 505 - 0.505: pnl = 3.995."""
        p = Portfolio(starting_quote={"exA": D("1000"), "exB": D("1000")})
        p.apply_buy("exA", "BTC", D("5"), D("500"), D("0.5"))
        p.transfer("exA", "exB", "BTC")
        pnl = p.apply_sell("exB", "BTC", D("5"), D("505"), D("0.505"))
        assert pnl == pytest.approx(D("3.995"), abs=D("1e-9"))
        assert p.realized_pnl_quote == pytest.approx(D("3.995"), abs=D("1e-9"))
        assert p.wins == 1 and p.losses == 0
        assert p.total_fees_quote == D("1.005")

    def test_losing_round_trip_counts_loss(self) -> None:
        p = Portfolio(starting_quote={"exA": D("1000")})
        p.apply_buy("exA", "BTC", D("1"), D("100"), D("0.1"))
        pnl = p.apply_sell("exA", "BTC", D("1"), D("99"), D("0.099"))
        assert pnl < 0
        assert p.losses == 1 and p.wins == 0

    def test_transfer_preserves_cost_basis(self) -> None:
        p = Portfolio(starting_quote={"exA": D("1000")})
        p.apply_buy("exA", "BTC", D("2"), D("200"), D("0.2"))
        moved = p.transfer("exA", "exB", "BTC")
        assert moved == D("2")
        assert p.position("exA", "BTC").base_amount == 0
        dst = p.position("exB", "BTC")
        assert dst.base_amount == D("2")
        assert dst.cost_quote == D("200.2")

    def test_transfer_missing_position_is_noop(self) -> None:
        p = Portfolio()
        assert p.transfer("exA", "exB", "BTC") == 0


class TestEquity:
    def test_equity_mark_to_mid_with_open_inventory(self) -> None:
        p = Portfolio(starting_quote={"exA": D("1000"), "exB": D("500")})
        p.apply_buy("exA", "BTC", D("2"), D("200"), D("0"))
        mids = {("exA", "BTC"): D("105")}
        # cash 800 + 500; inventory 2 * 105 = 210
        assert p.equity(mids) == D("1510")

    def test_equity_ignores_inventory_without_mid(self) -> None:
        p = Portfolio(starting_quote={"exA": D("1000")})
        p.apply_buy("exA", "BTC", D("2"), D("200"), D("0"))
        assert p.equity({}) == D("800")

    def test_equity_no_trades_equals_starting_cash(self) -> None:
        p = Portfolio(starting_quote={"exA": D("100"), "exB": D("100")})
        assert p.equity({}) == D("200")


class TestBalancesView:
    def test_balances_lists_quote_and_inventory(self) -> None:
        p = Portfolio(starting_quote={"exA": D("100")})
        p.apply_buy("exA", "BTC", D("1"), D("50"), D("0"))
        view = p.balances()
        assert view["exA"]["QUOTE"] == D("50")
        assert view["exA"]["BTC"] == D("1")

    def test_open_inventory_cost_sums_positions(self) -> None:
        p = Portfolio(starting_quote={"exA": D("1000")})
        p.apply_buy("exA", "BTC", D("1"), D("100"), D("0.5"))
        p.apply_buy("exA", "ETH", D("2"), D("60"), D("0.3"))
        assert p.open_inventory_cost() == D("160.8")
