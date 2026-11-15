"""Rich live terminal dashboard for spread monitoring."""

from __future__ import annotations

import time
from typing import Any

from rich.console import Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from connectors.base import BaseConnector
from core.models import ArbitrageOpportunity, OrderBookSnapshot


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
    ) -> None:
        self.connectors = connectors
        self.pairs = pairs
        self.volume_quote = volume_quote
        self.min_spread_bps = min_spread_bps
        self.interval = interval
        self.db_path = db_path
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
        return header

    def _liquidity_cell(self, opp: ArbitrageOpportunity) -> str:
        if opp.route.value != "cross_exchange":
            return "-"
        buy_snap = self._last_snaps.get((opp.buy_exchange, opp.pair))
        sell_snap = self._last_snaps.get((opp.sell_exchange, opp.pair))
        if buy_snap is None or sell_snap is None:
            return "-"
        return (
            f"{fmt_quote(float(buy_snap.ask_liquidity_quote()))}"
            f"/{fmt_quote(float(sell_snap.bid_liquidity_quote()))}"
        )

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
            cells = [
                Text(x, style=style)
                for x in (
                    opp.pair,
                    route,
                    f"{float(opp.gross_spread_bps):+.1f}",
                    f"{float(opp.fees_bps):.1f}",
                    f"{float(opp.slippage_bps):+.1f}",
                )
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

    def render(self) -> RenderableType:
        parts: list[RenderableType] = [self._header(), self._routes_table()]
        parts.extend(self._status_lines())
        return Panel(Group(*parts), title="cex-orderbook-arbitrage", border_style="blue")

    def start(self) -> Live:
        live = Live(self.render(), refresh_per_second=2, screen=False)
        live.start()
        return live
