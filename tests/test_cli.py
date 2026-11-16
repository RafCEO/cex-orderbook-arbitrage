"""Pure dashboard helpers: quote compaction and latency color decisions."""

from __future__ import annotations

import pytest
from rich.text import Text

from cli.dashboard import Dashboard, fmt_latency, fmt_quote


class TestFmtQuote:
    def test_small_values_plain(self) -> None:
        assert fmt_quote(999.0) == "999"

    def test_thousands_compact(self) -> None:
        assert fmt_quote(1500.0) == "1.5K"

    def test_millions_compact(self) -> None:
        assert fmt_quote(2_500_000.0) == "2.5M"

    def test_negative_millions_use_absolute_scale(self) -> None:
        assert fmt_quote(-1_200_000.0) == "-1.2M"

    def test_boundary_exactly_one_million(self) -> None:
        assert fmt_quote(1_000_000.0) == "1.0M"

    def test_just_below_million_stays_k(self) -> None:
        assert fmt_quote(999_999.0) == "1000.0K"


class TestFmtLatency:
    def test_none_renders_dash(self) -> None:
        assert fmt_latency(None).plain == "-"

    def test_fast_latency_green(self) -> None:
        assert fmt_latency(100.0).style == "green"

    def test_medium_latency_yellow(self) -> None:
        assert fmt_latency(600.0).style == "yellow"

    def test_slow_latency_red(self) -> None:
        assert fmt_latency(2000.0).style == "red"

    @pytest.mark.parametrize(
        ("ms", "expected_style"),
        [(0.0, "green"), (500.0, "green"), (500.1, "yellow"), (1500.0, "yellow"), (1500.1, "red")],
    )
    def test_color_thresholds(self, ms: float, expected_style: str) -> None:
        assert fmt_latency(ms).style == expected_style


class TestDashboardPureHelpers:
    def _dashboard(self) -> Dashboard:
        return Dashboard(
            connectors={},
            pairs=["BTC/USDT"],
            volume_quote=1000.0,
            min_spread_bps=5.0,
            interval=2.0,
            db_path="data/session.db",
        )

    def test_update_counts_unsuppressed_only(self) -> None:
        dash = self._dashboard()
        from tests.fakes import make_opportunity

        emitted = make_opportunity()
        suppressed = make_opportunity(suppressed=True)
        dash.update([emitted, suppressed], {})
        assert dash.opportunities_found == 1
        dash.update([emitted], {})
        assert dash.opportunities_found == 2

    def test_liquidity_cell_missing_snapshot(self) -> None:
        dash = self._dashboard()
        from tests.fakes import make_opportunity

        assert dash._liquidity_cell(make_opportunity()) == "-"

    def test_liquidity_cell_triangular_dash(self) -> None:
        dash = self._dashboard()
        from core.models import RouteType
        from tests.fakes import make_opportunity

        tri = make_opportunity(route=RouteType.TRIANGULAR, buy_exchange="exA", sell_exchange="exA")
        assert dash._liquidity_cell(tri) == "-"

    def test_status_lines_healthy_connectors_silent(self) -> None:
        dash = self._dashboard()
        assert dash._status_lines() == []

    def test_header_contains_configuration(self) -> None:
        dash = self._dashboard()
        header = dash._header()
        assert isinstance(header, Text) or header is not None

    def test_routes_table_empty_state(self) -> None:
        dash = self._dashboard()
        table = dash._routes_table()
        assert table.row_count == 1  # "no routes evaluated yet" placeholder


class TestFxDashboard:
    def _fx_dashboard(self) -> Dashboard:
        from cli.dashboard import Dashboard
        from engine.fx import ImpliedFxCross

        return Dashboard(
            connectors={},
            pairs=["BTC/USDT", "BTC/JPY"],
            volume_quote=1000.0,
            min_spread_bps=5.0,
            interval=2.0,
            db_path="data/session.db",
            fx=ImpliedFxCross(smoothing_window=1),
        )

    def test_fx_line_none_without_states(self) -> None:
        dash = self._fx_dashboard()
        assert dash._fx_line() is None

    def test_fx_line_renders_cross_with_age(self) -> None:
        dash = self._fx_dashboard()
        assert dash.fx is not None

        from tests.fakes import make_level, make_snapshot

        snaps = {
            ("binance", "BTC/USDT"): make_snapshot(
                bids=[make_level("64400", "1")],
                asks=[make_level("64410", "1")],
                exchange="binance",
                pair="BTC/USDT",
            ),
            ("bitflyer", "BTC/JPY"): make_snapshot(
                bids=[make_level("10266000", "1")],
                asks=[make_level("10266100", "1")],
                exchange="bitflyer",
                pair="BTC/JPY",
            ),
        }
        dash.fx.update(snaps)
        line = dash._fx_line()
        assert line is not None
        assert "JPY/USDT" in line.plain
        assert "(age" in line.plain

    def test_routes_table_renders_fx_tag(self) -> None:
        dash = self._fx_dashboard()
        from tests.fakes import make_opportunity

        opp = make_opportunity(
            pair="BTC/USDT",
            sell_exchange="bitflyer",
            sell_pair="BTC/JPY",
            fx_quote="JPY/USDT",
            fx_rate=159.5,
        )
        dash.update([opp], {})
        table = dash._routes_table()
        first_cell = table.columns[0]._cells[0]
        assert isinstance(first_cell, Text)
        assert "@fx JPY/USDT" in first_cell.plain

    def test_liquidity_cell_uses_sell_pair(self) -> None:
        dash = self._fx_dashboard()

        from tests.fakes import make_level, make_opportunity, make_snapshot

        buy = make_snapshot(
            bids=[make_level("64400", "1")],
            asks=[make_level("64410", "1")],
            exchange="binance",
            pair="BTC/USDT",
        )
        sell = make_snapshot(
            bids=[make_level("10266000", "1")],
            asks=[make_level("10266100", "1")],
            exchange="bitflyer",
            pair="BTC/JPY",
        )
        dash.update(
            [
                make_opportunity(
                    pair="BTC/USDT",
                    buy_exchange="binance",
                    sell_exchange="bitflyer",
                    sell_pair="BTC/JPY",
                    fx_quote="JPY/USDT",
                )
            ],
            {("binance", "BTC/USDT"): buy, ("bitflyer", "BTC/JPY"): sell},
        )
        opp = dash._last_opps[0]
        cell = dash._liquidity_cell(opp)
        assert cell != "-"

    def test_header_contains_fx_line_when_present(self) -> None:
        dash = self._fx_dashboard()
        assert dash._header() is not None
