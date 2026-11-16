"""Async REST connector base: rate limiting, retry with full-jitter backoff, latency tracking."""

from __future__ import annotations

import abc
import asyncio
import logging
import random
import time
from datetime import UTC, datetime
from decimal import Decimal

import aiohttp

from core.models import OrderBookLevel, OrderBookSnapshot

logger = logging.getLogger(__name__)

RETRY_BASE_DELAY: float = 0.5
RETRY_FACTOR: float = 2.0
RETRY_MAX_DELAY: float = 30.0
REQUEST_TIMEOUT: float = 10.0


class ConnectorError(Exception):
    """Raised when a connector exhausts its retry budget."""


class RateLimiter:
    """Token bucket: capacity == rate, refills at rate tokens/sec."""

    def __init__(self, rate_per_sec: float) -> None:
        self.rate = float(rate_per_sec)
        self.capacity = max(1.0, self.rate)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self.rate)


class BaseConnector(abc.ABC):
    """Shared aiohttp session, symbol mapping, retries and transport latency metric."""

    exchange_name: str = "base"
    default_rate_limit: float = 5.0
    quotes: tuple[str, ...] = ()

    def supports_quote(self, quote: str) -> bool:
        """True when the exchange lists the quote currency; empty tuple means unfiltered."""
        return not self.quotes or quote in self.quotes

    def __init__(
        self,
        session: aiohttp.ClientSession,
        depth: int = 50,
        rate_limit_per_sec: float | None = None,
        max_retries: int = 5,
    ) -> None:
        self.session = session
        self.depth = depth
        self.rate_limiter = RateLimiter(rate_limit_per_sec or self.default_rate_limit)
        self.max_retries = max_retries
        self.last_request_ms: float | None = None
        self.consecutive_failures = 0
        self.next_retry_not_before: float = 0.0
        self.last_error: str | None = None

    @classmethod
    def to_exchange_symbol(cls, pair: str) -> str:
        return pair.replace("/", "")

    @classmethod
    def to_canonical_pair(cls, raw: str) -> str:
        return raw

    @abc.abstractmethod
    async def fetch_orderbook(self, pair: str) -> OrderBookSnapshot:
        """Fetch and parse a full orderbook snapshot for a canonical pair."""

    async def start(self) -> None:
        if self.session.closed:
            raise ConnectorError(f"{self.exchange_name}: session closed before start")

    async def close(self) -> None:
        if not self.session.closed:
            await self.session.close()

    def _backoff_active(self) -> bool:
        return time.monotonic() < self.next_retry_not_before

    def _backoff_wait(self) -> float:
        return max(0.0, self.next_retry_not_before - time.monotonic())

    async def _get_json(self, url: str, params: dict[str, str] | None = None) -> dict:
        """GET with token bucket, timeout, full-jitter exponential backoff.

        delay_n = min(base * factor^n, cap); sleep = uniform(0, delay_n)
        """
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            if self._backoff_active():
                await asyncio.sleep(self._backoff_wait())
            await self.rate_limiter.acquire()
            started = time.monotonic()
            try:
                timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                async with self.session.get(url, params=params, timeout=timeout) as resp:
                    self.last_request_ms = (time.monotonic() - started) * 1000
                    if resp.status >= 400:
                        raise ConnectorError(f"{self.exchange_name}: HTTP {resp.status} for {url}")
                    payload: dict = await resp.json()
                self.consecutive_failures = 0
                self.last_error = None
                return payload
            except (TimeoutError, aiohttp.ClientError, ConnectorError) as exc:
                last_exc = exc
                self.consecutive_failures = attempt + 1
                self.last_error = str(exc)
                delay = min(RETRY_BASE_DELAY * RETRY_FACTOR**attempt, RETRY_MAX_DELAY)
                jittered = random.uniform(0.0, delay)
                self.next_retry_not_before = time.monotonic() + jittered
                logger.warning(
                    "%s: attempt %d/%d failed (%s), backoff %.2fs",
                    self.exchange_name,
                    attempt + 1,
                    self.max_retries,
                    exc,
                    jittered,
                )
                await asyncio.sleep(jittered)
        raise ConnectorError(
            f"{self.exchange_name}: {url} failed after {self.max_retries} retries: {last_exc}"
        )

    @staticmethod
    def _snapshot(
        exchange: str,
        pair: str,
        raw_bids: list[list[str]],
        raw_asks: list[list[str]],
        exchange_ts: datetime,
    ) -> OrderBookSnapshot:
        def levels(raw: list[list[str]]) -> list[OrderBookLevel]:
            return [OrderBookLevel(price=Decimal(p), amount=Decimal(q)) for p, q in raw]

        return OrderBookSnapshot(
            exchange=exchange,
            pair=pair,
            bids=levels(raw_bids),
            asks=levels(raw_asks),
            exchange_ts=exchange_ts,
            received_at=datetime.now(UTC),
        )
