"""Binance spot orderbook connector (public REST, no keys)."""

from __future__ import annotations

from datetime import UTC, datetime

from connectors.base import BaseConnector
from core.models import OrderBookSnapshot

BASE_URL = "https://api.binance.com/api/v3/depth"


class BinanceConnector(BaseConnector):
    exchange_name = "binance"
    default_rate_limit = 10.0
    quotes = ("USDT",)

    async def fetch_orderbook(self, pair: str) -> OrderBookSnapshot:
        symbol = self.to_exchange_symbol(pair)
        payload = await self._get_json(BASE_URL, {"symbol": symbol, "limit": str(self.depth)})
        if "bids" not in payload or "asks" not in payload:
            raise ValueError(f"binance: malformed depth payload for {pair}")
        epoch_ms = payload.get("E")
        exchange_ts = (
            datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
            if epoch_ms is not None
            else datetime.now(UTC)
        )
        return self._snapshot(
            exchange=self.exchange_name,
            pair=pair,
            raw_bids=[[p, q] for p, q in payload["bids"]],
            raw_asks=[[p, q] for p, q in payload["asks"]],
            exchange_ts=exchange_ts,
        )
