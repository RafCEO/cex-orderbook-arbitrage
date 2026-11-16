"""Rich live terminal dashboard for spread monitoring."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from rich.console import Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from connectors.base import BaseConnector
from core.models import ArbitrageOpportunity, OrderBookSnapshot
from engine.fx import ImpliedFxCross


def fmt_quote(v: float) -> str:
    for unit, div in (("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return f"{v:.0f}"


def fmt_latency(ms: float | None) -> Text:
    if ms is None:
        return Text("-", style="dim")
    style = "red" if ms > 1500 else ("yellow" if ms > 500 else "green")
    return Text(f"{ms:.0f}", style=style)


class Dashboard:
    """Rich Live dashboard: header summary, per-route table, exchange status lines."""

    def __init__(
        self,
        connectors: dict[str, BaseConnector],
        pairs: list[str],
        volume_quote: float,
        min_spread_bps: float,
        interval: float,
        db_path: str,
        fx: ImpliedFxCross | None = None,
    ) -> None:
        self.connectors = connectors
        self.pairs = pairs
        self.volume_quote = volume_quote
        self.min_spread_bps = min_spread_bps
        self.interval = interval
        self.db_path = db_path
        self.fx = fx
        self._started = time.monotonic()
        self._last_opps: list[ArbitrageOpportunity] = []
        self._last_snaps: dict[tuple[str, str], OrderBookSnapshot] = {}
        self.opportunities_found = 0

    def update(
        self,
        opps: list[ArbitrageOpportunity],
        snaps: dict[tuple[str, str], OrderBookSnapshot],
    ) -> None:
        self._last_opps = opps
        self._last_snaps = snaps
        self.opportunities_found += sum(1 for o in opps if not o.suppressed)

    def _header(self) -> Table:
        uptime = time.monotonic() - self._started
        header = Table.grid(expand=True)
        header.add_column(justify="left")
        header.add_column(justify="right")
        header.add_row(
            f"pairs {', '.join(self.pairs)}  volume {self.volume_quote:.0f}  "
            f"min spread {self.min_spread_bps:.1f} bps  interval {self.interval:.1f}s",
            f"uptime {int(uptime // 60)}m{int(uptime % 60)}s  "
            f"opportunities {self.opportunities_found}  db {self.db_path}",
        )
        fx_line = self._fx_line()
        if fx_line is not None:
            header.add_row(fx_line, "")
        return header

    def _fx_line(self) -> Text | None:
        """Current implied crosses with age, e.g. 'JPY/USDT 159.42 (age 0.4s)'."""
        if self.fx is None:
            return None
        cells: list[tuple[str, str]] = []
        now = datetime.now(UTC)
        for cross, state in sorted(self.fx._states.items()):
            if state.rate is None or state.updated_at is None:
                continue
            age = (now - state.updated_at).total_seconds()
            style = "dim" if state.degraded or state.stale else "cyan"
            cells.append((f"{cross} {state.rate:.2f} (age {age:.1f}s)", style))
        if not cells:
            return None
        parts: list[Text] = []
        for txt, style in cells:
            span = Text(txt, style=style)
            parts.extend([span, Text("  ")])
        line = Text.assemble(*parts)
        line.rstrip()
        return line

    def _liquidity_cell(self, opp: ArbitrageOpportunity) -> str:
        if opp.route.value != "cross_exchange":
            return "-"
        buy_snap = self._last_snaps.get((opp.buy_exchange, opp.pair))
        sell_snap = self._last_snaps.get((opp.sell_exchange, opp.sell_pair or opp.pair))
        if buy_snap is None or sell_snap is None:
            return "-"
        buy_liq = float(buy_snap.ask_liquidity_quote()) * self._fx_usdt(opp.buy_exchange)
        sell_liq = float(sell_snap.bid_liquidity_quote()) * self._fx_usdt(opp.sell_exchange)
        return f"{fmt_quote(buy_liq)}/{fmt_quote(sell_liq)}"

    def _fx_usdt(self, exchange: str) -> float:
        """Common-quote conversion factor for an exchange's quote currency."""
        if self.fx is None:
            return 1.0
        quotes = self.connectors[exchange].quotes if exchange in self.connectors else ()
        if not quotes or "USDT" in quotes:
            return 1.0
        asset = quotes[0]
        state = self.fx.state(f"USDT/{asset}")
        return float(state.rate) if state is not None and state.rate is not None else 1.0

    def _routes_table(self) -> Table:
        table = Table(box=None, expand=True, show_header=True)
        for col, kwargs in (
            ("pair", {"style": "bold", "no_wrap": True}),
            ("route", {"no_wrap": True}),
            ("gross bps", {"justify": "right", "no_wrap": True}),
            ("fees bps", {"justify": "right", "no_wrap": True}),
            ("slip bps", {"justify": "right", "no_wrap": True}),
            ("NET bps", {"justify": "right", "no_wrap": True}),
            ("fill %", {"justify": "right", "no_wrap": True}),
            ("liq buy/sell", {"justify": "right", "no_wrap": True}),
            ("lat buy/sell ms", {"justify": "right", "no_wrap": True}),
        ):
            table.add_column(col, **kwargs)
        for opp in self._last_opps:
            net = float(opp.net_spread_bps)
            if opp.suppressed:
                net_cell: Any = Text(f"{net:+.1f} (spike filter)", style="dim")
                style = "dim"
            else:
                style = "green" if net >= self.min_spread_bps else "white"
                net_cell = Text(f"{net:+.1f}", style=style)
            route = (
                f"{opp.buy_exchange} -> {opp.sell_exchange}"
                if opp.route.value == "cross_exchange"
                else f"tri {opp.buy_exchange}"
            )
            pair_label = f"{opp.pair} -> {opp.sell_pair}" if opp.fx_quote else opp.pair
            fx_tag = (
                Text(f" @fx {opp.fx_quote}", style="magenta")
                if opp.fx_quote
                else Text("", style=style)
            )
            cells = [
                Text.assemble(Text(pair_label, style=style), fx_tag),
                Text(route, style=style),
                Text(f"{float(opp.gross_spread_bps):+.1f}", style=style),
                Text(f"{float(opp.fees_bps):.1f}", style=style),
                Text(f"{float(opp.slippage_bps):+.1f}", style=style),
            ]
            table.add_row(
                *cells,
                net_cell,
                Text(f"{float(opp.fill_ratio) * 100:.0f}", style=style),
                Text(self._liquidity_cell(opp), style=style),
                Text.assemble(
                    fmt_latency(opp.buy_latency_ms), "/", fmt_latency(opp.sell_latency_ms)
                ),
            )
        if not self._last_opps:
            table.add_row(Text("no routes evaluated yet", style="dim"), *[""] * 8)
        return table

    def _status_lines(self) -> list[RenderableType]:
        lines: list[RenderableType] = []
        for name, connector in self.connectors.items():
            if connector.consecutive_failures > 0 or connector.last_error:
                wait = connector._backoff_wait()
                lines.append(
                    Text(
                        f"{name}: reconnecting "
                        f"(attempt {max(1, connector.consecutive_failures)}, backoff {wait:.1f}s)",
                        style="dim",
                    )
                )
        return lines

    def render(self, trading: RenderableType | None = None) -> RenderableType:
        parts: list[RenderableType] = [self._header(), self._routes_table()]
        if trading is not None:
            parts.append(trading)
        parts.extend(self._status_lines())
        return Panel(Group(*parts), title="cex-orderbook-arbitrage", border_style="blue")

    def start(self) -> Live:
        live = Live(self.render(), refresh_per_second=2, screen=False)
        live.start()
        return live
