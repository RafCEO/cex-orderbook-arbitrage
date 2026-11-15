"""Bybit v5 spot orderbook connector (public REST, no keys)."""

from __future__ import annotations

from datetime import UTC, datetime

from connectors.base import BaseConnector
from core.models import OrderBookSnapshot

BASE_URL = "https://api.bybit.com/v5/market/orderbook"


class BybitConnector(BaseConnector):
    exchange_name = "bybit"
    default_rate_limit = 5.0

    async def fetch_orderbook(self, pair: str) -> OrderBookSnapshot:
        symbol = self.to_exchange_symbol(pair)
        payload = await self._get_json(
            BASE_URL, {"category": "spot", "symbol": symbol, "limit": str(self.depth)}
        )
        if payload.get("retCode") != 0:
            raise ValueError(f"bybit: {payload.get('retMsg')} for {pair}")
        result = payload["result"]
        exchange_ts = datetime.fromtimestamp(result["ts"] / 1000, tz=UTC)
        return self._snapshot(
            exchange=self.exchange_name,
            pair=pair,
            raw_bids=[[p, q] for p, q in result["b"]],
            raw_asks=[[p, q] for p, q in result["a"]],
            exchange_ts=exchange_ts,
        )
