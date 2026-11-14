"""Kraken spot orderbook connector (public REST, no keys)."""
from __future__ import annotations

from datetime import datetime, timezone

from connectors.base import BaseConnector
from core.models import OrderBookSnapshot

BASE_URL = "https://api.kraken.com/0/public/Depth"


class KrakenConnector(BaseConnector):
    exchange_name = "kraken"
    default_rate_limit = 1.0

    @classmethod
    def to_exchange_symbol(cls, pair: str) -> str:
        base, quote = pair.split("/")
        base = {"BTC": "XBT"}.get(base, base)
        return f"{base}{quote}"

    @classmethod
    def to_canonical_pair(cls, raw: str) -> str:
        """Normalize Kraken symbols, both modern (XBTUSDT) and legacy (XXBTZUSDT)."""
        for quote in ("USDT", "USD", "EUR"):
            if raw.endswith(quote):
                base_part = raw[: -len(quote)]
                if base_part.endswith("Z") and len(base_part) > 3:
                    base_part = base_part[:-1]
                if base_part.startswith("X") and len(base_part) == 4:
                    base_part = base_part[1:]
                base = {"XBT": "BTC"}.get(base_part, base_part)
                return f"{base}/{quote}"
        return raw

    async def fetch_orderbook(self, pair: str) -> OrderBookSnapshot:
        symbol = self.to_exchange_symbol(pair)
        payload = await self._get_json(BASE_URL, {"pair": symbol, "count": str(self.depth)})
        result = payload.get("result", {})
        book_key = next((k for k in result if k != "last"), None)
        if book_key is None:
            raise ValueError(f"kraken: no depth key in response for {pair}")
        book = result[book_key]
        exchange_ts = datetime.now(timezone.utc)
        return self._snapshot(
            exchange=self.exchange_name,
            pair=pair,
            raw_bids=[[p, q] for p, q, _ in book["bids"]],
            raw_asks=[[p, q] for p, q, _ in book["asks"]],
            exchange_ts=exchange_ts,
        )
