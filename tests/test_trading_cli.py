"""Trading CLI helpers: mode gating, panel formatting, execution pipeline glue."""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

import pytest
from rich.text import Text

from cli.trading_panel import TradingPanel, fmt_amount, mode_badge, pnl_style
from execution.base import ExecutionStatus, OrderIntent
from execution.portfolio import Portfolio
from main import check_live_gate, pair_assets, resolve_mode, starting_portfolio
from tests.fakes import make_level, make_opportunity, make_snapshot


def make_args(mode: str | None = None, ack: bool = False) -> argparse.Namespace:
    return argparse.Namespace(mode=mode, ack_live_risk=ack)


class TestResolveMode:
    def test_cli_flag_wins(self) -> None:
        cfg = {"trading": {"mode": "paper"}}
        assert resolve_mode(make_args("live"), cfg) == "live"

    def test_config_default_when_flag_absent(self) -> None:
        cfg = {"trading": {"mode": "paper"}}
        assert resolve_mode(make_args(None), cfg) == "paper"

    def test_monitor_default(self) -> None:
        assert resolve_mode(make_args(None), {}) == "monitor"


class TestLiveGate:
    def test_live_without_ack_refused(self) -> None:
        reason = check_live_gate("live", make_args("live", ack=False))
        assert reason is not None and "ack-live-risk" in reason

    def test_live_without_keys_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BINANCE_API_KEY", raising=False)
        monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
        monkeypatch.delenv("BYBIT_API_KEY", raising=False)
        monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
        reason = check_live_gate("live", make_args("live", ack=True))
        assert reason is not None and "API" in reason

    def test_live_with_ack_and_keys_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BINANCE_API_KEY", "k")
        monkeypatch.setenv("BINANCE_API_SECRET", "s")
        assert check_live_gate("live", make_args("live", ack=True)) is None

    def test_non_live_modes_pass_without_gates(self) -> None:
        for mode in ("monitor", "paper", "testnet"):
            assert check_live_gate(mode, make_args(mode)) is None


class TestPairAssets:
    def test_simple_pair(self) -> None:
        assert pair_assets("BTC/USDT") == ("BTC", "USDT")


class TestStartingPortfolio:
    def test_defaults_per_exchange(self) -> None:
        cfg = {"trading": {"starting_balances": {"binance": 500}}}
        portfolio = starting_portfolio(cfg, ["binance", "kraken"])
        assert portfolio.quote_balance("binance") == Decimal("500")
        assert portfolio.quote_balance("kraken") == Decimal("1000")


class TestPanelFormatting:
    def test_fmt_amount_scales(self) -> None:
        assert fmt_amount(Decimal("1234.56")) == "1234.56"
        assert fmt_amount(Decimal("0.000123")) == "0.000123"
        assert fmt_amount(Decimal("999999")) == "999999"
        assert fmt_amount(Decimal("1000000")) == "1000000"

    def test_pnl_style_colors(self) -> None:
        assert pnl_style(1) == "green"
        assert pnl_style(-1) == "red"
        assert pnl_style(0) == "white"

    def test_mode_badge_styles(self) -> None:
        assert "red" in str(mode_badge("live").style)
        assert "cyan" in str(mode_badge("paper").style)
        assert "yellow" in str(mode_badge("testnet").style)


class TestTradingPanelRender:
    def test_empty_panel_renders_inactive(self) -> None:
        panel = TradingPanel()
        panel.update("monitor", None, [], False, None, {})
        assert isinstance(panel.render(), Text) or panel.render() is not None

    def test_panel_with_portfolio_and_fill(self) -> None:
        from execution.base import ExecutionReport, LegFill

        panel = TradingPanel()
        portfolio = Portfolio(starting_quote={"exA": Decimal("1000")})
        portfolio.apply_buy("exA", "BTC", Decimal("1"), Decimal("100"), Decimal("0.1"))
        intent = OrderIntent(
            pair="BTC/USDT",
            buy_exchange="exA",
            sell_exchange="exB",
            base_asset="BTC",
            quote_asset="USDT",
            budget_quote=Decimal("100"),
            expected_buy_vwap=Decimal("100"),
            expected_sell_vwap=Decimal("101"),
            expected_net_spread_bps=Decimal("10"),
        )
        report = ExecutionReport(
            intent=intent,
            status=ExecutionStatus.PARTIAL,
            mode="paper",
            buy_fill=LegFill(
                side="buy",
                exchange="exA",
                base_amount=Decimal("1"),
                vwap_price=Decimal("100"),
                filled_quote=Decimal("100"),
                fee_quote=Decimal("0.1"),
            ),
            sell_fill=None,
            realized_pnl_quote=Decimal("0"),
            simulated=True,
        )
        panel.update("paper", portfolio, [report], False, None, {("exA", "BTC"): Decimal("101")})
        assert panel.halted is False
        assert len(panel.recent_fills) == 1

    def test_panel_halted_state(self) -> None:
        panel = TradingPanel()
        panel.update("paper", None, [], True, "daily loss limit reached", {})
        header = panel._header_line()
        assert "HALTED" in header.plain


class TestExecuteOpportunitiesPipeline:
    async def test_confirmed_profitable_route_executes(self) -> None:
        from core.models import FeeSchedule
        from execution.paper_executor import PaperExecutor
        from execution.risk import RiskConfig, RiskGuard
        from main import execute_opportunities
        from storage.trades_logger import TradeLogger

        buy_book = make_snapshot(
            bids=[make_level("99", "5")], asks=[make_level("100", "5")], exchange="exA"
        )
        sell_book = make_snapshot(
            bids=[make_level("101", "5")], asks=[make_level("102", "5")], exchange="exB"
        )
        snaps = {("exA", "BTC/USDT"): buy_book, ("exB", "BTC/USDT"): sell_book}
        portfolio = Portfolio(starting_quote={"exA": Decimal("1000"), "exB": Decimal("1000")})
        executor = PaperExecutor(
            portfolio=portfolio, fees={"exA": FeeSchedule(), "exB": FeeSchedule()}
        )
        executor.books = snaps
        risk = RiskGuard(RiskConfig(max_trade_quote="1000", min_act_spread_bps="5"))
        logger = TradeLogger(db_path="/tmp/never.db")

        opp = make_opportunity(buy_exchange="exA", sell_exchange="exB", volume_quote=Decimal("500"))
        reports = await execute_opportunities([opp], snaps, executor, risk, portfolio, logger)
        assert len(reports) == 1
        assert reports[0].status == ExecutionStatus.FILLED
        assert risk.realized_pnl_quote > 0

    async def test_suppressed_and_triangular_skipped(self, tmp_path: Path) -> None:
        from core.models import FeeSchedule, RouteType
        from execution.paper_executor import PaperExecutor
        from execution.risk import RiskConfig, RiskGuard
        from main import execute_opportunities
        from storage.trades_logger import TradeLogger

        buy_book = make_snapshot(
            bids=[make_level("99", "5")], asks=[make_level("100", "5")], exchange="exA"
        )
        sell_book = make_snapshot(
            bids=[make_level("101", "5")], asks=[make_level("102", "5")], exchange="exB"
        )
        snaps = {("exA", "BTC/USDT"): buy_book, ("exB", "BTC/USDT"): sell_book}
        portfolio = Portfolio(starting_quote={"exA": Decimal("1000"), "exB": Decimal("1000")})
        executor = PaperExecutor(
            portfolio=portfolio, fees={"exA": FeeSchedule(), "exB": FeeSchedule()}
        )
        risk = RiskGuard(RiskConfig(max_trade_quote="1000", min_act_spread_bps="5"))
        logger = TradeLogger(db_path=str(tmp_path / "t.db"))

        suppressed = make_opportunity(suppressed=True)
        tri = make_opportunity(route=RouteType.TRIANGULAR)
        reports = await execute_opportunities(
            [suppressed, tri], snaps, executor, risk, portfolio, logger
        )
        assert reports == []

    async def test_risk_rejection_skips_execution(self, tmp_path: Path) -> None:
        from core.models import FeeSchedule
        from execution.paper_executor import PaperExecutor
        from execution.risk import RiskConfig, RiskGuard
        from main import execute_opportunities
        from storage.trades_logger import TradeLogger

        buy_book = make_snapshot(
            bids=[make_level("99", "5")], asks=[make_level("100", "5")], exchange="exA"
        )
        sell_book = make_snapshot(
            bids=[make_level("101", "5")], asks=[make_level("102", "5")], exchange="exB"
        )
        snaps = {("exA", "BTC/USDT"): buy_book, ("exB", "BTC/USDT"): sell_book}
        portfolio = Portfolio(starting_quote={"exA": Decimal("1000"), "exB": Decimal("1000")})
        executor = PaperExecutor(
            portfolio=portfolio,
            fees={"exA": FeeSchedule(), "exB": FeeSchedule()},
        )
        strict = RiskGuard(RiskConfig(max_trade_quote="10", min_act_spread_bps="5"))
        logger = TradeLogger(db_path=str(tmp_path / "t.db"))

        opp = make_opportunity(buy_exchange="exA", sell_exchange="exB", volume_quote=Decimal("500"))
        reports = await execute_opportunities([opp], snaps, executor, strict, portfolio, logger)
        assert reports == []
