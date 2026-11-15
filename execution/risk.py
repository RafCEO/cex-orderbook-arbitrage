"""Pre-trade risk checks and a daily-loss circuit breaker (pure, config-driven)."""

from __future__ import annotations

from decimal import Decimal

from execution.base import OrderIntent

DEFAULT_MAX_TRADE_QUOTE = Decimal("100")
DEFAULT_MAX_EXPOSURE_QUOTE = Decimal("500")
DEFAULT_DAILY_LOSS_LIMIT = Decimal("50")
DEFAULT_MIN_ACT_SPREAD_BPS = Decimal("5")

RISK_CONFIG_KEYS = (
    "max_trade_quote",
    "max_exposure_quote",
    "daily_loss_limit_quote",
    "min_act_spread_bps",
)


class RiskConfig:
    """Thresholds; quote-denominated, Decimal throughout."""

    def __init__(
        self,
        max_trade_quote: str | Decimal = DEFAULT_MAX_TRADE_QUOTE,
        max_exposure_quote: str | Decimal = DEFAULT_MAX_EXPOSURE_QUOTE,
        daily_loss_limit_quote: str | Decimal = DEFAULT_DAILY_LOSS_LIMIT,
        min_act_spread_bps: str | Decimal = DEFAULT_MIN_ACT_SPREAD_BPS,
    ) -> None:
        self.max_trade_quote = Decimal(str(max_trade_quote))
        self.max_exposure_quote = Decimal(str(max_exposure_quote))
        self.daily_loss_limit_quote = Decimal(str(daily_loss_limit_quote))
        self.min_act_spread_bps = Decimal(str(min_act_spread_bps))

    @classmethod
    def from_config(cls, cfg: dict[str, object]) -> RiskConfig:
        kwargs = {key: cfg[key] for key in RISK_CONFIG_KEYS if key in cfg and cfg[key] is not None}
        return cls(**kwargs)  # type: ignore[arg-type]


class RiskGuard:
    """Gatekeeper every intent must pass: caps, spread gate, daily loss breaker.

    Exposure = open inventory cost basis + pending intent size; the breaker
    trips when cumulative realized P&L (negative) crosses -daily_loss_limit
    and stays tripped for the session.
    """

    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self.halted = False
        self.halt_reason: str | None = None
        self.realized_pnl_quote = Decimal(0)

    def record_pnl(self, pnl_quote: Decimal) -> None:
        """Accumulate session realized P&L; trip the breaker past the loss limit."""
        self.realized_pnl_quote += pnl_quote
        limit = abs(self.config.daily_loss_limit_quote)
        if not self.halted and self.realized_pnl_quote <= -limit:
            self.halted = True
            self.halt_reason = (
                f"daily loss limit reached: realized {self.realized_pnl_quote} <= -{limit}"
            )

    def check(self, intent: OrderIntent, open_exposure_quote: Decimal) -> str | None:
        """Return a rejection reason, or None when the intent may proceed."""
        if self.halted:
            return f"halted: {self.halt_reason}"
        if intent.budget_quote <= 0:
            return "budget must be positive"
        if intent.budget_quote > self.config.max_trade_quote:
            return (
                f"budget {intent.budget_quote} exceeds max_trade_quote "
                f"{self.config.max_trade_quote}"
            )
        if open_exposure_quote + intent.budget_quote > self.config.max_exposure_quote:
            return (
                f"exposure {open_exposure_quote + intent.budget_quote} would exceed "
                f"max_exposure_quote {self.config.max_exposure_quote}"
            )
        if intent.expected_net_spread_bps < self.config.min_act_spread_bps:
            return (
                f"net spread {intent.expected_net_spread_bps} bps below "
                f"min_act_spread_bps {self.config.min_act_spread_bps}"
            )
        return None
