"""Gate.io v4 spot orderbook connector (public REST, no keys)."""

from __future__ import annotations

from datetime import UTC, datetime

from connectors.base import BaseConnector
from core.models import OrderBookSnapshot

BASE_URL = "https://api.gateio.ws/api/v4/spot/order_book"


class GateConnector(BaseConnector):
    exchange_name = "gate"
    default_rate_limit = 8.0
    quotes = ("USDT",)

    @classmethod
    def to_exchange_symbol(cls, pair: str) -> str:
        return pair.replace("/", "_")

    @classmethod
    def to_canonical_pair(cls, raw: str) -> str:
        base, _, quote = raw.partition("_")
        return f"{base}/{quote}" if quote else raw

    async def fetch_orderbook(self, pair: str) -> OrderBookSnapshot:
        symbol = self.to_exchange_symbol(pair)
        payload = await self._get_json(
            BASE_URL, {"currency_pair": symbol, "limit": str(self.depth)}
        )
        if "bids" not in payload or "asks" not in payload:
            raise ValueError(f"gate: malformed depth payload for {pair}")
        updated = payload.get("update") or payload.get("current")
        exchange_ts = (
            datetime.fromtimestamp(int(updated) / 1000, tz=UTC) if updated else datetime.now(UTC)
        )
        return self._snapshot(
            exchange=self.exchange_name,
            pair=pair,
            raw_bids=[row[:2] for row in payload["bids"]],
            raw_asks=[row[:2] for row in payload["asks"]],
            exchange_ts=exchange_ts,
        )
