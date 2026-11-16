"""KuCoin spot orderbook connector (public REST, no keys).

The documented /api/v1/market/orderbook/level2_50 endpoint returns 404 on the
current API; the level2_100 shape is identical, so depth is sliced locally.
"""

from __future__ import annotations

from datetime import UTC, datetime

from connectors.base import BaseConnector
from core.models import OrderBookSnapshot

BASE_URL = "https://api.kucoin.com/api/v1/market/orderbook/level2_100"


class KucoinConnector(BaseConnector):
    exchange_name = "kucoin"
    default_rate_limit = 8.0

    @classmethod
    def to_exchange_symbol(cls, pair: str) -> str:
        return pair.replace("/", "-")

    @classmethod
    def to_canonical_pair(cls, raw: str) -> str:
        base, _, quote = raw.partition("-")
        return f"{base}/{quote}" if quote else raw

    async def fetch_orderbook(self, pair: str) -> OrderBookSnapshot:
        symbol = self.to_exchange_symbol(pair)
        payload = await self._get_json(BASE_URL, {"symbol": symbol})
        data = payload.get("data") or {}
        if payload.get("code") != "200000" or "bids" not in data:
            raise ValueError(f"kucoin: malformed depth payload for {pair}")
        exchange_ts = (
            datetime.fromtimestamp(int(data["time"]) / 1000, tz=UTC)
            if data.get("time")
            else datetime.now(UTC)
        )
        return self._snapshot(
            exchange=self.exchange_name,
            pair=pair,
            raw_bids=[row[:2] for row in data["bids"][: self.depth]],
            raw_asks=[row[:2] for row in data["asks"][: self.depth]],
            exchange_ts=exchange_ts,
        )
