"""Trading dashboard panel: mode badge, balances, P&L, open inventory, fills feed."""

from __future__ import annotations

from decimal import Decimal

from rich.table import Table
from rich.text import Text

from execution.base import ExecutionReport
from execution.portfolio import Portfolio

MODE_STYLES: dict[str, str] = {
    "monitor": "dim",
    "paper": "cyan",
    "testnet": "yellow",
    "live": "red bold",
}


def fmt_amount(v: Decimal | float) -> str:
    value = float(v)
    if value != 0 and abs(value) < 0.01:
        return f"{value:.6f}"
    if abs(value) >= 100000:
        return f"{value:.0f}"
    return f"{value:.2f}"


def pnl_style(value: Decimal | float) -> str:
    v = float(value)
    if v > 0:
        return "green"
    if v < 0:
        return "red"
    return "white"


def mode_badge(mode: str) -> Text:
    return Text(mode.upper(), style=MODE_STYLES.get(mode, "white"))


class TradingPanel:
    """Positions/P&L panel rendered under the spread table when trading is active."""

    MAX_FILLS = 8

    def __init__(self) -> None:
        self.portfolio: Portfolio | None = None
        self.mode = "monitor"
        self.halted = False
        self.halt_reason: str | None = None
        self.recent_fills: list[ExecutionReport] = []
        self.quote_asset = "USDT"
        self.mids: dict[tuple[str, str], Decimal] = {}
        self.fx_rates: dict[str, Decimal] = {}

    def update(
        self,
        mode: str,
        portfolio: Portfolio | None,
        reports: list[ExecutionReport],
        halted: bool,
        halt_reason: str | None,
        mids: dict[tuple[str, str], Decimal],
        fx_rates: dict[str, Decimal] | None = None,
    ) -> None:
        self.mode = mode
        self.portfolio = portfolio
        self.halted = halted
        self.halt_reason = halt_reason
        self.mids = mids
        self.fx_rates = fx_rates or {}
        executed = [r for r in reports if r.status.value != "rejected"]
        self.recent_fills = (self.recent_fills + executed)[-self.MAX_FILLS :]

    def _header_line(self) -> Text:
        if self.halted:
            return Text.assemble(
                mode_badge(self.mode),
                Text("  HALTED: ", style="red bold"),
                Text(self.halt_reason or "circuit breaker tripped", style="red"),
            )
        return Text.assemble(mode_badge(self.mode), Text(" simulated fills", style="dim"))

    def _balances_table(self) -> Table:
        table = Table(box=None, expand=True, show_header=True, padding=(0, 1))
        table.add_column("exchange", no_wrap=True)
        table.add_column("quote", justify="right", no_wrap=True)
        table.add_column("open inventory", justify="right", no_wrap=True)
        p = self.portfolio
        if p is None:
            table.add_row(Text("trading inactive", style="dim"), "", "")
            return table
        for exchange in sorted(p.quote.keys()):
            inventory = sum(
                (pos.base_amount for (ex, _base), pos in p.inventory.items() if ex == exchange),
                Decimal(0),
            )
            table.add_row(
                Text(exchange, style="bold"),
                Text(
                    f"{fmt_amount(p.quote_balance(exchange))} {p.quote_asset(exchange)}",
                ),
                Text(fmt_amount(inventory), style="cyan" if inventory > 0 else "dim"),
            )
        return table

    def _pnl_table(self) -> Table:
        table = Table.grid(expand=True)
        table.add_column(justify="left")
        table.add_column(justify="right")
        p = self.portfolio
        if p is None:
            table.add_row(Text("trading inactive", style="dim"), Text(""))
            return table
        inventory_value = p.open_inventory_value(self.mids, self.fx_rates)
        inventory_cost = p.open_inventory_cost()
        unrealized = inventory_value - inventory_cost if p.inventory else Decimal(0)
        equity = p.equity(self.mids, self.fx_rates)
        trades = p.wins + p.losses
        win_rate = (p.wins / trades * 100) if trades else 0.0
        rows = [
            (
                "realized P&L",
                Text(
                    f"{fmt_amount(p.realized_pnl_quote)}",
                    style=pnl_style(p.realized_pnl_quote),
                ),
            ),
            (
                "unrealized",
                Text(f"{fmt_amount(unrealized)}", style=pnl_style(unrealized)),
            ),
            ("equity", Text(f"{fmt_amount(equity)}", style="white")),
            (
                "trades",
                Text(
                    f"{trades}  win {win_rate:.0f}%  W{p.wins}/L{p.losses}",
                    style="white",
                ),
            ),
            (
                "fees paid",
                Text(f"{fmt_amount(p.total_fees_quote)}", style="yellow"),
            ),
        ]
        for label, value in rows:
            table.add_row(Text(label, style="dim"), value)
        return table

    def _inventory_table(self) -> Table:
        table = Table(box=None, expand=True, show_header=True, padding=(0, 1))
        table.add_column("position", no_wrap=True)
        table.add_column("amount", justify="right", no_wrap=True)
        table.add_column("avg cost", justify="right", no_wrap=True)
        table.add_column("value", justify="right", no_wrap=True)
        p = self.portfolio
        if p is None or not p.inventory:
            table.add_row(Text("no open inventory", style="dim"), "", "", "")
            return table
        for (exchange, base_asset), pos in sorted(p.inventory.items()):
            mid = self.mids.get((exchange, base_asset))
            value = pos.base_amount * mid if mid is not None else None
            table.add_row(
                Text(f"{exchange} {base_asset}", style="bold"),
                Text(fmt_amount(pos.base_amount), style="cyan"),
                Text(fmt_amount(pos.avg_cost)),
                Text(fmt_amount(value) if value is not None else "-", style="dim"),
            )
        return table

    def _fills_table(self) -> Table:
        table = Table(box=None, expand=True, show_header=True, padding=(0, 1))
        table.add_column("time", no_wrap=True)
        table.add_column("route", no_wrap=True)
        table.add_column("base", justify="right", no_wrap=True)
        table.add_column("pnl", justify="right", no_wrap=True)
        if not self.recent_fills:
            table.add_row(Text("no fills yet", style="dim"), "", "", "")
            return table
        for report in reversed(self.recent_fills):
            base = report.closed_base
            table.add_row(
                Text(report.completed_at.strftime("%H:%M:%S"), style="dim"),
                Text(
                    f"{report.intent.buy_exchange}->{report.intent.sell_exchange}",
                    style="white",
                ),
                Text(fmt_amount(base)),
                Text(
                    f"{fmt_amount(report.realized_pnl_quote)}",
                    style=pnl_style(report.realized_pnl_quote),
                ),
            )
        return table

    def render(self) -> Table:
        grid = Table.grid(expand=True, padding=(0, 2))
        grid.add_column(ratio=1)
        grid.add_column(ratio=1)
        grid.add_row(self._balances_table(), self._pnl_table())
        grid.add_row(self._inventory_table(), self._fills_table())
        header = Table.grid(expand=True)
        header.add_column(justify="left")
        header.add_row(self._header_line())
        outer = Table.grid(expand=True)
        outer.add_column()
        outer.add_row(header)
        outer.add_row(grid)
        return outer
