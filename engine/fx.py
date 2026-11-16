"""Market-implied FX cross rates between quote currencies, no external FX feed.

For an ordered quote pair (target per source, e.g. JPY per USDT) the rate is
derived from a reference base asset traded against both quotes:

    rate = mid(base/target) / mid(base/source)

using the freshest snapshot per quote. Raw rates are smoothed with a rolling
median over the last N ticks so a single thin-book print cannot whipsaw the
cross. Two guards protect route economics:

    staleness : inputs older than max_age_ms are ignored, and a cross whose
                last update is older than max_age_ms is not usable
    sanity    : a raw rate deviating more than sanity_band_bps from the median
                of the smoothing history marks the cross degraded (last good
                median is kept); while degraded, no cross-quote opportunities
                are emitted so a broken conversion never looks like free money

Converting proceeds across quotes is never free: callers apply
conversion_haircut_bps on top of the FX spread, approximating the cost of
moving value between stablecoin and fiat rails.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from core.models import OrderBookSnapshot

BPS = Decimal(10_000)

DEFAULT_REFERENCE_BASE = "BTC"
DEFAULT_SMOOTHING_WINDOW = 5
DEFAULT_SANITY_BAND_BPS = 200.0
DEFAULT_CONVERSION_HAIRCUT_BPS = 25.0
DEFAULT_MAX_AGE_MS = 5000.0
COMMON_QUOTE = "USDT"


def quote_of(pair: str) -> str:
    return pair.rsplit("/", 1)[1]


@dataclass
class CrossState:
    """Observable state of one implied cross: smoothed rate plus health flags."""

    rate: Decimal | None = None
    updated_at: datetime | None = None
    degraded: bool = False
    stale: bool = True
    history: deque[Decimal] = field(default_factory=deque)


def _median(values: deque[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


class ImpliedFxCross:
    """Rolling implied cross rates for every ordered quote pair seen in ticks."""

    def __init__(
        self,
        reference_base: str = DEFAULT_REFERENCE_BASE,
        smoothing_window: int = DEFAULT_SMOOTHING_WINDOW,
        sanity_band_bps: float = DEFAULT_SANITY_BAND_BPS,
        conversion_haircut_bps: float = DEFAULT_CONVERSION_HAIRCUT_BPS,
        max_age_ms: float = DEFAULT_MAX_AGE_MS,
    ) -> None:
        self.reference_base = reference_base
        self.smoothing_window = smoothing_window
        self.sanity_band_bps = sanity_band_bps
        self.conversion_haircut_bps = Decimal(str(conversion_haircut_bps))
        self.max_age_ms = max_age_ms
        self._states: dict[str, CrossState] = {}

    def _update_cross(self, cross: str, raw_rate: Decimal, now: datetime) -> CrossState:
        state = self._states.setdefault(cross, CrossState())
        reference = _median(state.history) if len(state.history) >= 2 else None
        band = Decimal(str(self.sanity_band_bps)) / BPS
        if reference is not None and reference != 0:
            deviation = abs(raw_rate - reference) / reference
        else:
            deviation = None
        if deviation is not None and deviation > band:
            state.degraded = True
            state.updated_at = now
            return state
        state.degraded = False
        state.history.append(raw_rate)
        if len(state.history) > self.smoothing_window:
            state.history.popleft()
        state.rate = _median(state.history)
        state.stale = False
        state.updated_at = now
        return state

    def update(self, snaps: dict[tuple[str, str], OrderBookSnapshot]) -> dict[str, CrossState]:
        """Recompute every (target/source) cross implied by the freshest snapshots."""
        now = datetime.now(UTC)
        freshest: dict[str, tuple[datetime, Decimal]] = {}
        for (_exchange, pair), snap in snaps.items():
            base, quote = pair.split("/")
            mid = snap.mid_price
            if base != self.reference_base or mid is None or mid <= 0:
                continue
            if snap.age_ms > self.max_age_ms:
                continue
            current = freshest.get(quote)
            if current is None or snap.received_at > current[0]:
                freshest[quote] = (snap.received_at, mid)
        quotes = sorted(freshest)
        for i, target in enumerate(quotes):
            for source in quotes[i + 1 :]:
                mid_t = freshest[target][1]
                mid_s = freshest[source][1]
                self._update_cross(f"{target}/{source}", mid_t / mid_s, now)
                self._update_cross(f"{source}/{target}", mid_s / mid_t, now)
        return self._states

    def state(self, cross: str) -> CrossState | None:
        return self._states.get(cross)

    def rate(self, cross: str) -> Decimal | None:
        state = self._states.get(cross)
        return None if state is None else state.rate

    def usable(self, cross: str, now: datetime | None = None) -> bool:
        """True when the cross has a fresh, non-degraded rate."""
        state = self._states.get(cross)
        if state is None or state.rate is None or state.stale or state.degraded:
            return False
        if state.updated_at is None:
            return False
        reference = now or datetime.now(UTC)
        return (reference - state.updated_at).total_seconds() * 1000 <= self.max_age_ms

    def convert(self, amount: Decimal, source_quote: str, target_quote: str) -> Decimal | None:
        """Convert a source-quote amount into target-quote units at the smoothed rate."""
        if source_quote == target_quote:
            return amount
        rate = self.rate(f"{target_quote}/{source_quote}")
        return None if rate is None else amount * rate

    def haircut_mult(self) -> Decimal:
        """Multiplier applied to proceeds converted across quotes (haircut < 1)."""
        return Decimal(1) - self.conversion_haircut_bps / BPS
