"""Paper executor: simulates both legs against the live orderbooks the monitor fetched."""

from __future__ import annotations

from decimal import Decimal

from core.models import ArbitrageOpportunity, FeeSchedule, OrderBookSnapshot
from engine.fx import ImpliedFxCross
from engine.vwap_calculator import walk_depth, walk_depth_base
from execution.base import (
    BaseExecutor,
    ExecutionReport,
    ExecutionStatus,
    LegFill,
    OrderIntent,
    build_intent,
    pnl_from_legs,
)
from execution.portfolio import Portfolio

BPS = Decimal(10_000)


def taker_fee(fee: FeeSchedule, filled_quote: Decimal) -> Decimal:
    return filled_quote * fee.taker_bps / BPS


class PaperExecutor(BaseExecutor):
    """Fills a two-leg taker round trip by walking fetched books (no keys, no orders).

    Buy leg walks real asks up to the quote budget (capped by available quote
    balance); sell leg walks real bids for the bought base amount. Each leg
    pays its exchange's taker fee on its notional. Fills are conservative:
    walked VWAP, never better than top-of-book, partial when books are shallow.
    Moving base between exchanges is simulated as free and instant. Cross-quote
    routes fill each leg natively in its own quote; the round trip leaves
    inventory in the sell leg's quote, and realized P&L converts through the
    supplied implied FX rates.
    """

    def __init__(
        self,
        portfolio: Portfolio,
        fees: dict[str, FeeSchedule],
        fx: ImpliedFxCross | None = None,
    ) -> None:
        super().__init__()
        self.portfolio = portfolio
        self.fees = fees
        self.fx = fx
        self.reports: list[ExecutionReport] = []

    @property
    def name(self) -> str:
        return "paper"

    def build_intent(
        self,
        opp: ArbitrageOpportunity,
        buy_snap: OrderBookSnapshot,
        sell_snap: OrderBookSnapshot,
        base_asset: str,
        quote_asset: str,
    ) -> OrderIntent | None:
        return build_intent(opp, buy_snap, sell_snap, base_asset, quote_asset)

    async def execute(self, intent: OrderIntent) -> ExecutionReport:
        buy_snap = self.books.get((intent.buy_exchange, intent.pair))
        sell_snap = self.books.get((intent.sell_exchange, intent.sell_pair or intent.pair))
        if buy_snap is None or sell_snap is None:
            return self._reject(intent, f"missing book for {intent.pair}")
        fee_buy = self.fees.get(intent.buy_exchange, FeeSchedule())
        fee_sell = self.fees.get(intent.sell_exchange, FeeSchedule())

        available = self.portfolio.quote_balance(intent.buy_exchange)
        fee_rate = fee_buy.taker_bps / BPS
        required = intent.budget_quote * (Decimal(1) + fee_rate)
        if available < required:
            return self._reject(
                intent,
                f"insufficient {intent.quote_asset} on {intent.buy_exchange}: "
                f"have {available}, need {required}",
            )

        buy_fill = walk_depth(buy_snap.asks, intent.budget_quote)
        if buy_fill.base_filled <= 0 or buy_fill.vwap_price is None:
            return self._reject(intent, "empty buy book")
        buy_fee = taker_fee(fee_buy, buy_fill.filled_quote)
        self.portfolio.apply_buy(
            exchange=intent.buy_exchange,
            base_asset=intent.base_asset,
            base_amount=buy_fill.base_filled,
            filled_quote=buy_fill.filled_quote,
            fee_quote=buy_fee,
        )
        self.portfolio.transfer(intent.buy_exchange, intent.sell_exchange, intent.base_asset)

        sell_fill = walk_depth_base(sell_snap.bids, buy_fill.base_filled)
        if sell_fill.base_filled <= 0 or sell_fill.vwap_price is None:
            return self._reject(intent, "empty sell book after buy")
        sell_fee = taker_fee(fee_sell, sell_fill.filled_quote)
        self.portfolio.apply_sell(
            exchange=intent.sell_exchange,
            base_asset=intent.base_asset,
            base_amount=sell_fill.base_filled,
            filled_quote=sell_fill.filled_quote,
            fee_quote=sell_fee,
            fx_rates=self._fx_rates_for(intent),
        )

        buy_leg = LegFill(
            side="buy",
            exchange=intent.buy_exchange,
            base_amount=buy_fill.base_filled,
            vwap_price=buy_fill.vwap_price,
            filled_quote=buy_fill.filled_quote,
            fee_quote=buy_fee,
        )
        sell_leg = LegFill(
            side="sell",
            exchange=intent.sell_exchange,
            base_amount=sell_fill.base_filled,
            vwap_price=sell_fill.vwap_price,
            filled_quote=sell_fill.filled_quote,
            fee_quote=sell_fee,
        )
        fully_filled = (
            buy_fill.fill_ratio >= Decimal(1) and sell_fill.base_filled == buy_fill.base_filled
        )
        rates = self._fx_rates_for(intent)
        report = ExecutionReport(
            intent=intent,
            status=ExecutionStatus.FILLED if fully_filled else ExecutionStatus.PARTIAL,
            mode="paper",
            buy_fill=buy_leg,
            sell_fill=sell_leg,
            fees_quote=buy_fee + sell_fee,
            realized_pnl_quote=pnl_from_legs(
                buy_leg,
                sell_leg,
                self._leg_rate(intent.buy_exchange, rates),
                self._leg_rate(intent.sell_exchange, rates),
            ),
            simulated=True,
            submitted_at=intent.detected_at,
        )
        self.reports.append(report)
        return report

    def _fx_rates_for(self, intent: OrderIntent) -> dict[str, Decimal]:
        """Common-quote units per quote asset, derived from the executor's FX cross."""
        if self.fx is None:
            return {}
        out: dict[str, Decimal] = {}
        for exchange in (intent.buy_exchange, intent.sell_exchange):
            asset = self.portfolio.quote_asset(exchange)
            if asset == self.portfolio.common_quote or asset in out:
                continue
            rate = self.fx.rate(f"{self.portfolio.common_quote}/{asset}")
            if rate is not None:
                out[asset] = rate
        return out

    def _leg_rate(self, exchange: str, rates: dict[str, Decimal]) -> Decimal:
        """Conversion rate for an exchange whose quote the rates dict knows."""
        asset = self.portfolio.quote_asset(exchange)
        return rates.get(asset, Decimal(1))

    def _reject(self, intent: OrderIntent, reason: str) -> ExecutionReport:
        report = ExecutionReport(
            intent=intent,
            status=ExecutionStatus.REJECTED,
            mode="paper",
            simulated=True,
            reason=reason,
            submitted_at=intent.detected_at,
        )
        self.reports.append(report)
        return report

    async def balances(self) -> dict[str, dict[str, Decimal]]:
        return self.portfolio.balances()
