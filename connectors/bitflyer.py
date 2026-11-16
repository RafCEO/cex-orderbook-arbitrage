"""bitFlyer spot board connector (public REST, no keys).

Board prices and sizes arrive as JSON numbers; Decimal(str(...)) preserves the
exact digits the exchange sent. No server timestamp in the response, so
receive time is used as exchange_ts (same estimate as Kraken).
"""

from __future__ import annotations

from datetime import UTC, datetime

from connectors.base import BaseConnector
from core.models import OrderBookSnapshot

BASE_URL = "https://api.bitflyer.com/v1/board"


class BitflyerConnector(BaseConnector):
    exchange_name = "bitflyer"
    default_rate_limit = 1.5
    quotes = ("JPY",)

    @classmethod
    def to_exchange_symbol(cls, pair: str) -> str:
        return pair.replace("/", "_")

    @classmethod
    def to_canonical_pair(cls, raw: str) -> str:
        base, _, quote = raw.partition("_")
        return f"{base}/{quote}" if quote else raw

    async def fetch_orderbook(self, pair: str) -> OrderBookSnapshot:
        symbol = self.to_exchange_symbol(pair)
        payload = await self._get_json(BASE_URL, {"product_code": symbol})
        if "bids" not in payload or "asks" not in payload:
            raise ValueError(f"bitflyer: malformed board payload for {pair}")
        return self._snapshot(
            exchange=self.exchange_name,
            pair=pair,
            raw_bids=[[str(lv["price"]), str(lv["size"])] for lv in payload["bids"]],
            raw_asks=[[str(lv["price"]), str(lv["size"])] for lv in payload["asks"]],
            exchange_ts=datetime.now(UTC),
        )
