"""RiskGuard: per-trade cap, exposure cap, spread gate, daily loss breaker."""

from __future__ import annotations

from decimal import Decimal

from execution.base import OrderIntent
from execution.risk import RiskConfig, RiskGuard


def make_intent(
    budget: str = "100",
    net_bps: str = "10",
    buy_exchange: str = "exA",
    sell_exchange: str = "exB",
) -> OrderIntent:
    return OrderIntent(
        pair="BTC/USDT",
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        base_asset="BTC",
        quote_asset="USDT",
        budget_quote=Decimal(budget),
        expected_buy_vwap=Decimal("100"),
        expected_sell_vwap=Decimal("101"),
        expected_net_spread_bps=Decimal(net_bps),
    )


def make_guard(**overrides: str) -> RiskGuard:
    defaults = {
        "max_trade_quote": "100",
        "max_exposure_quote": "500",
        "daily_loss_limit_quote": "50",
        "min_act_spread_bps": "5",
    }
    defaults.update(overrides)
    return RiskGuard(RiskConfig(**defaults))


class TestPerTradeCap:
    def test_budget_above_cap_rejected(self) -> None:
        guard = make_guard()
        assert guard.check(make_intent(budget="100.01"), Decimal(0)) is not None

    def test_budget_at_cap_passes(self) -> None:
        guard = make_guard()
        assert guard.check(make_intent(budget="100"), Decimal(0)) is None

    def test_zero_budget_rejected(self) -> None:
        guard = make_guard()
        assert guard.check(make_intent(budget="0"), Decimal(0)) is not None


class TestExposureCap:
    def test_exposure_above_cap_rejected(self) -> None:
        guard = make_guard()
        assert guard.check(make_intent(budget="100"), Decimal("401")) is not None

    def test_exposure_at_cap_passes(self) -> None:
        guard = make_guard()
        assert guard.check(make_intent(budget="100"), Decimal("400")) is None


class TestSpreadGate:
    def test_below_min_act_spread_rejected(self) -> None:
        guard = make_guard()
        assert guard.check(make_intent(net_bps="4.99"), Decimal(0)) is not None

    def test_at_min_act_spread_passes(self) -> None:
        guard = make_guard()
        assert guard.check(make_intent(net_bps="5"), Decimal(0)) is None

    def test_negative_spread_rejected(self) -> None:
        guard = make_guard(min_act_spread_bps="-100")
        assert guard.check(make_intent(net_bps="-100.01"), Decimal(0)) is not None


class TestDailyLossBreaker:
    def test_breaker_trips_at_limit_and_stays(self) -> None:
        guard = make_guard()
        guard.record_pnl(Decimal("-20"))
        assert not guard.halted
        guard.record_pnl(Decimal("-30"))
        assert guard.halted
        assert guard.halt_reason is not None and "daily loss" in guard.halt_reason
        # stays tripped even after a win
        guard.record_pnl(Decimal("500"))
        assert guard.halted

    def test_halted_guard_blocks_every_intent(self) -> None:
        guard = make_guard()
        guard.record_pnl(Decimal("-50"))
        reason = guard.check(make_intent(), Decimal(0))
        assert reason is not None and reason.startswith("halted")

    def test_losses_above_limit_trip_immediately(self) -> None:
        guard = make_guard()
        guard.record_pnl(Decimal("-49.999"))
        assert not guard.halted

    def test_boundary_exactly_at_limit_trips(self) -> None:
        guard = make_guard()
        guard.record_pnl(Decimal("-50"))
        assert guard.halted


class TestConfigParsing:
    def test_from_config_reads_keys(self) -> None:
        cfg: dict[str, object] = {
            "max_trade_quote": 250.0,
            "max_exposure_quote": 1000.0,
            "daily_loss_limit_quote": 75.0,
            "min_act_spread_bps": 8.0,
        }
        config = RiskConfig.from_config(cfg)
        assert config.max_trade_quote == Decimal("250.0")
        assert config.min_act_spread_bps == Decimal("8.0")

    def test_from_config_partial_keys_fall_back(self) -> None:
        config = RiskConfig.from_config({"max_trade_quote": "42"})
        assert config.max_trade_quote == Decimal("42")
        assert config.daily_loss_limit_quote == Decimal("50")

    def test_rejection_messages_are_informative(self) -> None:
        guard = make_guard()
        reason = guard.check(make_intent(budget="500"), Decimal(0))
        assert reason is not None and "max_trade_quote" in reason
        reason = guard.check(make_intent(net_bps="1"), Decimal(0))
        assert reason is not None and "min_act_spread_bps" in reason
