"""bitbank public depth connector (public REST, no keys)."""

from __future__ import annotations

from datetime import UTC, datetime

from connectors.base import BaseConnector
from core.models import OrderBookSnapshot

BASE_URL = "https://public.bitbank.cc"


class BitbankConnector(BaseConnector):
    exchange_name = "bitbank"
    default_rate_limit = 5.0
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
        payload = await self._get_json(f"{BASE_URL}/{symbol}/depth")
        data = payload.get("data") or {}
        if payload.get("success") != 1 or "asks" not in data:
            raise ValueError(f"bitbank: malformed depth payload for {pair}")
        ts = data.get("timestamp") or data.get("server_time")
        exchange_ts = datetime.fromtimestamp(int(ts) / 1000, tz=UTC) if ts else datetime.now(UTC)
        return self._snapshot(
            exchange=self.exchange_name,
            pair=pair,
            raw_bids=[row[:2] for row in data["bids"]],
            raw_asks=[row[:2] for row in data["asks"]],
            exchange_ts=exchange_ts,
        )
