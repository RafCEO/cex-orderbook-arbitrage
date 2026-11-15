"""Virtual balances, inventory and realized/unrealized P&L for simulated trading."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

DEFAULT_START_BALANCE = Decimal("1000")


@dataclass
class Position:
    """Open base-asset inventory on one exchange, all-in cost basis (fill + fee)."""

    base_amount: Decimal = Decimal(0)
    cost_quote: Decimal = Decimal(0)

    @property
    def avg_cost(self) -> Decimal:
        if self.base_amount == 0:
            return Decimal(0)
        return self.cost_quote / self.base_amount


@dataclass
class TradeRecord:
    """One completed round trip as booked by the portfolio."""

    pair: str
    buy_exchange: str
    sell_exchange: str
    base_amount: Decimal
    buy_vwap: Decimal
    sell_vwap: Decimal
    buy_fee_quote: Decimal
    sell_fee_quote: Decimal
    realized_pnl_quote: Decimal
    status: str


class Portfolio:
    """Quote balances per exchange plus base inventory; source of truth for paper P&L.

    Round trip (transfer between exchanges simulated, transfer cost not modeled):
        buy  : quote[buy_ex]  -= filled_quote + fee;   inventory[buy_ex]  += base
        move : inventory[buy_ex] -> inventory[sell_ex] (cost basis carries over)
        sell : inventory[sell_ex] -= sold;  quote[sell_ex] += filled_quote - fee
    Position cost basis is all-in (fill + fee), so round-trip realized P&L equals
    (sell_proceeds_net - buy_cost_net) exactly.
    """

    def __init__(self, starting_quote: dict[str, Decimal] | None = None) -> None:
        self.quote: dict[str, Decimal] = dict(starting_quote or {})
        self.inventory: dict[tuple[str, str], Position] = {}
        self.realized_pnl_quote = Decimal(0)
        self.total_fees_quote = Decimal(0)
        self.trades: list[TradeRecord] = []
        self.wins = 0
        self.losses = 0

    # -- balance access -------------------------------------------------------

    def quote_balance(self, exchange: str) -> Decimal:
        return self.quote.get(exchange, Decimal(0))

    def position(self, exchange: str, base_asset: str) -> Position:
        return self.inventory.get((exchange, base_asset), Position())

    def balances(self) -> dict[str, dict[str, Decimal]]:
        """Per-exchange {asset: amount}: quote asset keyed 'QUOTE' plus base inventory."""
        out: dict[str, dict[str, Decimal]] = {}
        for exchange, amount in self.quote.items():
            out.setdefault(exchange, {})["QUOTE"] = amount
        for (exchange, base_asset), pos in self.inventory.items():
            out.setdefault(exchange, {})[base_asset] = pos.base_amount
        return out

    # -- trade application ----------------------------------------------------

    def apply_buy(
        self,
        exchange: str,
        base_asset: str,
        base_amount: Decimal,
        filled_quote: Decimal,
        fee_quote: Decimal,
    ) -> None:
        if exchange not in self.quote:
            self.quote[exchange] = Decimal(0)
        self.quote[exchange] -= filled_quote + fee_quote
        pos = self.inventory.get((exchange, base_asset))
        if pos is None:
            pos = Position()
            self.inventory[(exchange, base_asset)] = pos
        pos.base_amount += base_amount
        pos.cost_quote += filled_quote + fee_quote
        self.total_fees_quote += fee_quote

    def transfer(self, from_exchange: str, to_exchange: str, base_asset: str) -> Decimal:
        """Move the full position between exchanges; returns transferred base amount."""
        pos = self.inventory.pop((from_exchange, base_asset), None)
        if pos is None or pos.base_amount <= 0:
            return Decimal(0)
        dst = self.inventory.setdefault((to_exchange, base_asset), Position())
        dst.base_amount += pos.base_amount
        dst.cost_quote += pos.cost_quote
        return pos.base_amount

    def apply_sell(
        self,
        exchange: str,
        base_asset: str,
        base_amount: Decimal,
        filled_quote: Decimal,
        fee_quote: Decimal,
    ) -> Decimal:
        """Close inventory against a sell fill; returns realized P&L of the closed portion.

        pnl = (filled_quote - fee) * closed/sold - closed * avg_cost
        where avg_cost is the all-in (fill + fee) buy basis.
        """
        if exchange not in self.quote:
            self.quote[exchange] = Decimal(0)
        self.quote[exchange] += filled_quote - fee_quote
        self.total_fees_quote += fee_quote
        pos = self.inventory.get((exchange, base_asset))
        if pos is None:
            return Decimal(0)
        closed_base = min(base_amount, pos.base_amount)
        if closed_base <= 0 or base_amount <= 0:
            return Decimal(0)
        proceeds_net = (filled_quote - fee_quote) * (closed_base / base_amount)
        avg_cost = pos.avg_cost
        pnl = proceeds_net - closed_base * avg_cost
        pos.base_amount -= closed_base
        pos.cost_quote -= closed_base * avg_cost
        if pos.base_amount <= 0:
            del self.inventory[(exchange, base_asset)]
        self.realized_pnl_quote += pnl
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1
        return pnl

    # -- valuation --------------------------------------------------------------

    def open_inventory_value(self, mids: dict[tuple[str, str], Decimal]) -> Decimal:
        """Mark-to-mid value of open inventory; mids keyed (exchange, base_asset)."""
        return sum(
            (
                pos.base_amount * mid
                for (exchange, base), pos in self.inventory.items()
                if (mid := mids.get((exchange, base))) is not None
            ),
            Decimal(0),
        )

    def open_inventory_cost(self) -> Decimal:
        return sum((pos.cost_quote for pos in self.inventory.values()), Decimal(0))

    def equity(self, mids: dict[tuple[str, str], Decimal]) -> Decimal:
        return sum(self.quote.values(), Decimal(0)) + self.open_inventory_value(mids)
