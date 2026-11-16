"""Coincheck order books connector (public REST, no keys).

Levels may arrive unsorted; OrderBookSnapshot's validator sorts and sanitizes
both sides. No timestamp in the response, so receive time is used as
exchange_ts.
"""

from __future__ import annotations

from datetime import UTC, datetime

from connectors.base import BaseConnector
from core.models import OrderBookSnapshot

BASE_URL = "https://coincheck.com/api/order_books"


class CoincheckConnector(BaseConnector):
    exchange_name = "coincheck"
    default_rate_limit = 1.5
    quotes = ("JPY",)

    @classmethod
    def to_exchange_symbol(cls, pair: str) -> str:
        return pair.replace("/", "_").lower()

    @classmethod
    def to_canonical_pair(cls, raw: str) -> str:
        base, _, quote = raw.upper().partition("_")
        return f"{base}/{quote}" if quote else raw

    async def fetch_orderbook(self, pair: str) -> OrderBookSnapshot:
        symbol = self.to_exchange_symbol(pair)
        payload = await self._get_json(BASE_URL, {"pair": symbol})
        if "bids" not in payload or "asks" not in payload:
            raise ValueError(f"coincheck: malformed order books payload for {pair}")
        return self._snapshot(
            exchange=self.exchange_name,
            pair=pair,
            raw_bids=[row[:2] for row in payload["bids"]],
            raw_asks=[row[:2] for row in payload["asks"]],
            exchange_ts=datetime.now(UTC),
        )
