"""Test doubles: deterministic clock, snapshot factories and a scripted aiohttp session."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import Any, cast

import aiohttp

from core.models import (
    ArbitrageOpportunity,
    OrderBookLevel,
    OrderBookSnapshot,
    RouteType,
)

DEFAULT_FROZEN_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class FrozenDateTime:
    """datetime stand-in whose now() is pinned; tests move it explicitly."""

    frozen: datetime = DEFAULT_FROZEN_NOW

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return cls.frozen if tz is None else cls.frozen.astimezone(tz)


class FrozenClock:
    """Controller paired with FrozenDateTime: pins and advances the wall clock."""

    def __init__(self) -> None:
        FrozenDateTime.frozen = DEFAULT_FROZEN_NOW

    @property
    def now(self) -> datetime:
        return FrozenDateTime.frozen

    def set(self, dt: datetime) -> None:
        FrozenDateTime.frozen = dt

    def advance(self, seconds: float) -> None:
        FrozenDateTime.frozen += timedelta(seconds=seconds)


def make_level(price: str | int | Decimal, amount: str | int | Decimal) -> OrderBookLevel:
    return OrderBookLevel(price=Decimal(str(price)), amount=Decimal(str(amount)))


def make_snapshot(
    bids: list[OrderBookLevel] | None = None,
    asks: list[OrderBookLevel] | None = None,
    *,
    exchange: str = "exA",
    pair: str = "BTC/USDT",
    latency_ms: float = 0.0,
    age_ms: float = 0.0,
    received_at: datetime | None = None,
    liquidity_depth: int = 10,
) -> OrderBookSnapshot:
    """Deterministic snapshot: received_at defaults to now minus age_ms."""
    received = received_at or datetime.now(UTC) - timedelta(milliseconds=age_ms)
    return OrderBookSnapshot(
        exchange=exchange,
        pair=pair,
        bids=bids or [],
        asks=asks or [],
        exchange_ts=received - timedelta(milliseconds=latency_ms),
        received_at=received,
        liquidity_depth=liquidity_depth,
    )


def make_opportunity(
    *,
    net_spread_bps: str = "10",
    fill_ratio: str = "1",
    **overrides: Any,
) -> ArbitrageOpportunity:
    defaults: dict[str, Any] = {
        "route": RouteType.CROSS_EXCHANGE,
        "pair": "BTC/USDT",
        "buy_exchange": "exA",
        "sell_exchange": "exB",
        "volume_quote": Decimal(1000),
        "gross_spread_bps": Decimal(30),
        "slippage_bps": Decimal(5),
        "fees_bps": Decimal(15),
        "net_spread_bps": Decimal(net_spread_bps),
        "net_delta_quote": Decimal("1.0"),
        "fill_ratio": Decimal(fill_ratio),
        "buy_latency_ms": 10.0,
        "sell_latency_ms": 12.0,
        "suppressed": False,
    }
    defaults.update(overrides)
    return ArbitrageOpportunity(**defaults)


@dataclass
class FakeResponse:
    """The slice of aiohttp.ClientResponse the connectors consume."""

    payload: Any = None
    status: int = 200

    async def json(self, **_: Any) -> Any:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status >= 400:
            # ClientResponseError is only constructed to exercise the slice of
            # the aiohttp API connectors may consume; build its metadata fields.
            from multidict import CIMultiDict, CIMultiDictProxy
            from yarl import URL

            raise aiohttp.ClientResponseError(
                request_info=aiohttp.RequestInfo(
                    url=URL("https://test/api"),
                    method="GET",
                    headers=CIMultiDictProxy(CIMultiDict()),
                    real_url=URL("https://test/api"),
                ),
                history=(),
                status=self.status,
                message="http error",
            )


class _RequestContext:
    """Async context manager mimicking aiohttp session.get(...) usage."""

    def __init__(self, outcome: FakeResponse | BaseException) -> None:
        self.outcome = outcome

    async def __aenter__(self) -> FakeResponse:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class FakeAiohttpSession:
    """Scripted aiohttp.ClientSession: pops one outcome per GET and records calls."""

    def __init__(self, outcomes: list[FakeResponse | BaseException] | None = None) -> None:
        self.outcomes: list[FakeResponse | BaseException] = list(outcomes or [])
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def get(self, url: str, *, params: dict[str, str] | None = None, **_: Any) -> _RequestContext:
        self.calls.append({"url": url, "params": dict(params or {})})
        outcome = self.outcomes.pop(0) if self.outcomes else FakeResponse(payload={})
        return _RequestContext(outcome)

    async def close(self) -> None:
        self.closed = True


def as_client_session(fake: FakeAiohttpSession) -> aiohttp.ClientSession:
    """Cast the double to ClientSession: it implements the whole API slice used."""
    return cast(aiohttp.ClientSession, fake)
