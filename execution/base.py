"""Execution interface: order intents derived from routes and their fill reports."""

from __future__ import annotations

import abc
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from core.models import ArbitrageOpportunity, OrderBookSnapshot
from engine.vwap_calculator import walk_depth, walk_depth_base


class ExecutionError(Exception):
    """Raised when a signed order request fails at the transport or exchange layer."""


class LegFill(BaseModel):
    """One executed leg of a two-leg round trip.

    fee_quote = filled_quote * taker_bps / 10_000
    """

    model_config = ConfigDict(strict=True)

    side: str
    exchange: str
    base_amount: Decimal
    vwap_price: Decimal
    filled_quote: Decimal
    fee_quote: Decimal


class ExecutionStatus(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"


class OrderIntent(BaseModel):
    """Route extracted from a confirmed opportunity, ready for an executor.

    budget_quote caps the buy leg; expected VWAPs come from walking the books
    that produced the opportunity so executors can bracket protective limit
    prices around them.
    """

    model_config = ConfigDict(strict=True)

    pair: str
    buy_exchange: str
    sell_exchange: str
    base_asset: str
    quote_asset: str
    budget_quote: Decimal
    expected_buy_vwap: Decimal
    expected_sell_vwap: Decimal
    expected_net_spread_bps: Decimal
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ExecutionReport(BaseModel):
    """Outcome of one intent: per-leg fills, fees and the matched-portion P&L.

    realized_pnl_quote counts only the round-tripped portion:
        pnl = closed_base * (sell_net_rate - buy_net_rate), rates net of fees
    """

    model_config = ConfigDict(strict=True)

    intent: OrderIntent
    status: ExecutionStatus
    mode: str
    buy_fill: LegFill | None = None
    sell_fill: LegFill | None = None
    fees_quote: Decimal = Decimal(0)
    realized_pnl_quote: Decimal = Decimal(0)
    simulated: bool = False
    reason: str | None = None
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def closed_base(self) -> Decimal:
        buy = self.buy_fill.base_amount if self.buy_fill is not None else Decimal(0)
        sell = self.sell_fill.base_amount if self.sell_fill is not None else Decimal(0)
        return min(buy, sell)


def build_intent(
    opp: ArbitrageOpportunity,
    buy_snap: OrderBookSnapshot,
    sell_snap: OrderBookSnapshot,
    base_asset: str,
    quote_asset: str,
) -> OrderIntent | None:
    """Walk both books at the opportunity volume to attach expected leg VWAPs."""
    buy_walk = walk_depth(buy_snap.asks, opp.volume_quote)
    if buy_walk.vwap_price is None or buy_walk.base_filled <= 0:
        return None
    sell_walk = walk_depth_base(sell_snap.bids, buy_walk.base_filled)
    if sell_walk.vwap_price is None or sell_walk.base_filled <= 0:
        return None
    return OrderIntent(
        pair=opp.pair,
        buy_exchange=opp.buy_exchange,
        sell_exchange=opp.sell_exchange,
        base_asset=base_asset,
        quote_asset=quote_asset,
        budget_quote=opp.volume_quote,
        expected_buy_vwap=buy_walk.vwap_price,
        expected_sell_vwap=sell_walk.vwap_price,
        expected_net_spread_bps=opp.net_spread_bps,
        detected_at=opp.detected_at,
    )


def pnl_from_legs(buy: LegFill, sell: LegFill | None) -> Decimal:
    """Matched-portion realized P&L; both legs' rates are fee-inclusive."""
    if sell is None or sell.base_amount <= 0 or buy.base_amount <= 0:
        return Decimal(0)
    buy_net_rate = (buy.filled_quote + buy.fee_quote) / buy.base_amount
    sell_net_rate = (sell.filled_quote - sell.fee_quote) / sell.base_amount
    return min(buy.base_amount, sell.base_amount) * (sell_net_rate - buy_net_rate)


class BaseExecutor(abc.ABC):
    """Common executor contract: execute an intent, introspect balances.

    books maps (exchange, pair) -> latest snapshot; the orchestrator refreshes
    it every tick so executors fill against current depth.
    """

    def __init__(self) -> None:
        self.books: dict[tuple[str, str], OrderBookSnapshot] = {}

    @abc.abstractmethod
    async def execute(self, intent: OrderIntent) -> ExecutionReport: ...

    @abc.abstractmethod
    async def balances(self) -> dict[str, dict[str, Decimal]]: ...

    @property
    @abc.abstractmethod
    def name(self) -> str: ...
