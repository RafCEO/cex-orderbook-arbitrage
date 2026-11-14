"""Strict domain models for orderbook snapshots, depth fills and arbitrage routes."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RouteType(str, Enum):
    CROSS_EXCHANGE = "cross_exchange"
    TRIANGULAR = "triangular"


class OrderBookLevel(BaseModel):
    """Single price level: notional = price * amount (quote-currency value)."""

    model_config = ConfigDict(strict=True)

    price: Decimal
    amount: Decimal

    @property
    def notional(self) -> Decimal:
        return self.price * self.amount


class OrderBookSnapshot(BaseModel):
    """Resting orderbook state for one (exchange, pair) at a point in time.

    Derived properties:
        latency_ms = (received_at - exchange_ts) * 1000        # exchange clock skew + network
        age_ms     = (now - received_at) * 1000                # local staleness
        mid_price  = (top_bid + top_ask) / 2
        spread_bps = (ask - bid) / mid * 10_000
        liquidity  = sum(price_i * amount_i) over top N levels
    """

    model_config = ConfigDict(strict=True, arbitrary_types_allowed=True)

    exchange: str
    pair: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    exchange_ts: datetime
    received_at: datetime
    liquidity_depth: int = 10

    @model_validator(mode="after")
    def _sanitize(self) -> Self:
        self.bids = sorted(
            (lv for lv in self.bids if lv.price > 0 and lv.amount > 0),
            key=lambda lv: lv.price,
            reverse=True,
        )
        self.asks = sorted(
            (lv for lv in self.asks if lv.price > 0 and lv.amount > 0),
            key=lambda lv: lv.price,
        )
        return self

    @property
    def latency_ms(self) -> float:
        return (self.received_at - self.exchange_ts).total_seconds() * 1000

    @property
    def age_ms(self) -> float:
        return (datetime.now(timezone.utc) - self.received_at).total_seconds() * 1000

    @property
    def mid_price(self) -> Decimal | None:
        if not self.bids or not self.asks:
            return None
        return (self.top_bid + self.top_ask) / 2  # type: ignore[operator]

    @property
    def top_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def top_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread_bps(self) -> Decimal | None:
        mid = self.mid_price
        if mid is None or mid == 0:
            return None
        return (self.top_ask - self.top_bid) / mid * Decimal(10_000)  # type: ignore[operator]

    def bid_liquidity_quote(self, depth: int | None = None) -> Decimal:
        n = self.liquidity_depth if depth is None else depth
        return sum((lv.notional for lv in self.bids[:n]), Decimal(0))

    def ask_liquidity_quote(self, depth: int | None = None) -> Decimal:
        n = self.liquidity_depth if depth is None else depth
        return sum((lv.notional for lv in self.asks[:n]), Decimal(0))


class FeeSchedule(BaseModel):
    """Taker/maker fees in basis points of notional."""

    maker_bps: Decimal = Decimal(0)
    taker_bps: Decimal = Decimal(0)


class FillResult(BaseModel):
    """Outcome of walking one side of the book to fill a quote-volume target.

    vwap_price = sum(price_i * amount_i) / sum(amount_i) over consumed levels
    fill_ratio = filled_quote / requested_quote
    residual_quote = requested_quote - filled_quote (unfilled remainder)
    """

    model_config = ConfigDict(strict=True)

    requested_quote: Decimal
    filled_quote: Decimal
    base_filled: Decimal
    vwap_price: Decimal | None
    levels_consumed: int
    fill_ratio: Decimal
    residual_quote: Decimal


class ArbitrageOpportunity(BaseModel):
    """Evaluated route with all-in economics.

    net_spread_bps = gross_spread_bps - slippage_bps - fees_bps
    net_delta_quote = sell_proceeds_net - buy_cost_net (quote currency)
    """

    route: RouteType
    pair: str
    buy_exchange: str
    sell_exchange: str
    volume_quote: Decimal
    gross_spread_bps: Decimal
    slippage_bps: Decimal
    fees_bps: Decimal
    net_spread_bps: Decimal
    net_delta_quote: Decimal
    fill_ratio: Decimal
    buy_latency_ms: float
    sell_latency_ms: float
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    suppressed: bool = False

    @property
    def is_profitable(self) -> bool:
        return self.net_spread_bps > 0 and self.fill_ratio >= Decimal("0.9")
