"""Spread detection: latency guard, cross-exchange and triangular route scanning, spike filter.

Rolling-median spike filter, per route:
    median_m = median(net_bps readings in last W ticks)
    suppress reading r if |r - median_m| > D AND route has not yet shown
    net >= min_spread for C consecutive ticks. A route becomes "confirmed"
    once C consecutive readings satisfy net >= min_spread_bps; a confirmed
    route bypasses the deviation test so persistent shifts are emitted.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from core.models import (
    ArbitrageOpportunity,
    FeeSchedule,
    OrderBookSnapshot,
)
from engine.fx import ImpliedFxCross, quote_of
from engine.vwap_calculator import (
    evaluate_cross_exchange,
    evaluate_cross_quote,
    evaluate_triangular,
)

DEFAULT_MAX_AGE_MS: float = 5000.0
DEFAULT_SPIKE_WINDOW: int = 20
DEFAULT_SPIKE_DEVIATION_BPS: float = 25.0
DEFAULT_CONFIRMATIONS: int = 2
DEFAULT_MIN_FILL_RATIO: float = 0.9


class SpikeFilterState:
    """Per-route rolling window and confirmation counter (pure state machine)."""

    def __init__(
        self,
        window: int = DEFAULT_SPIKE_WINDOW,
        deviation_bps: float = DEFAULT_SPIKE_DEVIATION_BPS,
        confirmations: int = DEFAULT_CONFIRMATIONS,
        min_spread_bps: float = 0.0,
    ) -> None:
        self.window = window
        self.deviation_bps = deviation_bps
        self.confirmations = confirmations
        self.min_spread_bps = min_spread_bps
        self.readings: deque[Decimal] = deque(maxlen=window)
        self.streak = 0

    def _median(self) -> Decimal | None:
        if not self.readings:
            return None
        ordered = sorted(self.readings)
        n = len(ordered)
        mid = n // 2
        if n % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2

    def accept(self, net_bps: Decimal) -> bool:
        """Record a reading; True when it passes the spike filter."""
        median = self._median()
        above_min = net_bps >= Decimal(str(self.min_spread_bps))
        if above_min:
            self.streak += 1
        else:
            self.streak = 0
        confirmed = self.streak >= self.confirmations
        is_outlier = median is not None and abs(net_bps - median) > Decimal(str(self.deviation_bps))
        allow = confirmed or not is_outlier
        if allow:
            self.readings.append(net_bps)
        return allow


class SpreadDetector:
    """Consumes per-tick snapshots; emits filtered opportunities (no asyncio, pure)."""

    def __init__(
        self,
        min_spread_bps: float = 5.0,
        max_snapshot_age_ms: float = DEFAULT_MAX_AGE_MS,
        min_fill_ratio: float = DEFAULT_MIN_FILL_RATIO,
        spike_window: int = DEFAULT_SPIKE_WINDOW,
        spike_deviation_bps: float = DEFAULT_SPIKE_DEVIATION_BPS,
        confirmations: int = DEFAULT_CONFIRMATIONS,
        fx: ImpliedFxCross | None = None,
    ) -> None:
        self.min_spread_bps = Decimal(str(min_spread_bps))
        self.max_snapshot_age_ms = max_snapshot_age_ms
        self.min_fill_ratio = Decimal(str(min_fill_ratio))
        self.spike_window = spike_window
        self.spike_deviation_bps = spike_deviation_bps
        self.confirmations = confirmations
        self.fx = fx
        self._filters: dict[tuple[str, str, str, str], SpikeFilterState] = {}

    def _fresh(self, snap: OrderBookSnapshot) -> bool:
        return snap.age_ms <= self.max_snapshot_age_ms

    def _evaluate_route(
        self,
        buy: OrderBookSnapshot,
        sell: OrderBookSnapshot,
        volume_quote: Decimal,
        fees: dict[str, FeeSchedule],
    ) -> ArbitrageOpportunity | None:
        """Evaluate one route; cross-quote legs are normalized through the FX cross."""
        buy_quote = quote_of(buy.pair)
        sell_quote = quote_of(sell.pair)
        buy_fee = fees.get(buy.exchange, FeeSchedule())
        sell_fee = fees.get(sell.exchange, FeeSchedule())
        if buy_quote == sell_quote:
            return evaluate_cross_exchange(buy, sell, volume_quote, buy_fee, sell_fee)
        if self.fx is None:
            return None
        cross = f"{buy_quote}/{sell_quote}"
        if not self.fx.usable(cross):
            return None
        return evaluate_cross_quote(
            buy,
            sell,
            volume_quote,
            buy_fee,
            sell_fee,
            self.fx.rate(cross) or Decimal(0),
            self.fx.conversion_haircut_bps,
        )

    def _route_filter(self, key: tuple[str, str, str, str]) -> SpikeFilterState:
        if key not in self._filters:
            self._filters[key] = SpikeFilterState(
                window=self.spike_window,
                deviation_bps=self.spike_deviation_bps,
                confirmations=self.confirmations,
                min_spread_bps=float(self.min_spread_bps),
            )
        return self._filters[key]

    def evaluate_tick(
        self,
        snapshots: dict[tuple[str, str], OrderBookSnapshot],
        volume_quote: Decimal,
        fees: dict[str, FeeSchedule],
        triplets: list[tuple[str, str, str]] | None = None,
    ) -> list[ArbitrageOpportunity]:
        """Evaluate one tick of (exchange, pair) -> snapshot; returns emitted opportunities."""
        out: list[ArbitrageOpportunity] = []
        fresh = {k: s for k, s in snapshots.items() if self._fresh(s)}

        by_base: dict[str, list[OrderBookSnapshot]] = {}
        for (_exchange, pair), snap in fresh.items():
            base = pair.split("/")[0]
            by_base.setdefault(base, []).append(snap)

        for _base, snaps in by_base.items():
            if len(snaps) < 2:
                continue
            for i, buy in enumerate(snaps):
                for j, sell in enumerate(snaps):
                    if i == j:
                        continue
                    opp = self._evaluate_route(buy, sell, volume_quote, fees)
                    if opp is None:
                        continue
                    key = (buy.pair, opp.buy_exchange, opp.sell_exchange, "cross")
                    emitted = self._route_filter(key).accept(opp.net_spread_bps)
                    if emitted:
                        out.append(opp)
                    else:
                        opp.suppressed = True
                        out.append(opp)

        for triplet in triplets or []:
            pair_a, pair_b, pair_c = triplet
            by_exchange: dict[str, list[OrderBookSnapshot | None]] = {}
            for exchange in {e for e, p in fresh if p in triplet}:
                by_exchange[exchange] = [
                    fresh.get((exchange, pair_a)),
                    fresh.get((exchange, pair_b)),
                    fresh.get((exchange, pair_c)),
                ]
            for exchange, legs in by_exchange.items():
                if any(leg is None for leg in legs):
                    continue
                a, b, c = (leg for leg in legs if leg is not None)
                fee = fees.get(exchange, FeeSchedule())
                opp = evaluate_triangular(a, b, c, volume_quote, fee)
                if opp is None:
                    continue
                key = (opp.pair, exchange, exchange, "tri")
                emitted = self._route_filter(key).accept(opp.net_spread_bps)
                if emitted:
                    out.append(opp)
                else:
                    opp.suppressed = True
                    out.append(opp)
        return out
