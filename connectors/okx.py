"""OKX v5 spot orderbook connector (public REST, no keys)."""

from __future__ import annotations

from datetime import UTC, datetime

from connectors.base import BaseConnector
from core.models import OrderBookSnapshot

BASE_URL = "https://www.okx.com/api/v5/market/books"


class OkxConnector(BaseConnector):
    exchange_name = "okx"
    default_rate_limit = 10.0

    @classmethod
    def to_exchange_symbol(cls, pair: str) -> str:
        return pair.replace("/", "-")

    @classmethod
    def to_canonical_pair(cls, raw: str) -> str:
        base, _, quote = raw.partition("-")
        return f"{base}/{quote}" if quote else raw

    async def fetch_orderbook(self, pair: str) -> OrderBookSnapshot:
        symbol = self.to_exchange_symbol(pair)
        payload = await self._get_json(BASE_URL, {"instId": symbol, "sz": str(self.depth)})
        if payload.get("code") != "0" or not payload.get("data"):
            raise ValueError(f"okx: {payload.get('msg')} for {pair}")
        book = payload["data"][0]
        ts = book.get("ts")
        exchange_ts = datetime.fromtimestamp(int(ts) / 1000, tz=UTC) if ts else datetime.now(UTC)
        return self._snapshot(
            exchange=self.exchange_name,
            pair=pair,
            raw_bids=[[p, q] for p, q, _liq, _orders in book["bids"]],
            raw_asks=[[p, q] for p, q, _liq, _orders in book["asks"]],
            exchange_ts=exchange_ts,
        )
