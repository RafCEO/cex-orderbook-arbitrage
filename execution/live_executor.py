"""Signed order placement for Binance and Bybit spot (Kraken trading intentionally unsupported).

Orders are LIMIT IOC at the walked-VWAP price padded by a configurable buffer
for price protection; naked market orders are opt-in via config. Testnet and
live share this executor, differing only in base URL and the credential gate.
"""

from __future__ import annotations

import os
from decimal import ROUND_DOWN, Decimal

import aiohttp

from execution.auth import binance_signed_request, bybit_signed_request
from execution.base import (
    BaseExecutor,
    ExecutionError,
    ExecutionReport,
    ExecutionStatus,
    LegFill,
    OrderIntent,
    pnl_from_legs,
)

BINANCE_LIVE_URL = "https://api.binance.com"
BINANCE_TESTNET_URL = "https://testnet.binance.vision"
BYBIT_LIVE_URL = "https://api.bybit.com"
BYBIT_TESTNET_URL = "https://api-testnet.bybit.com"

SUPPORTED_EXCHANGES = ("binance", "bybit")
KRAKEN_UNSUPPORTED = "kraken trading is intentionally unsupported (no testnet)"

PRICE_DECIMALS = Decimal("0.00000001")
AMOUNT_DECIMALS = Decimal("0.000001")
REQUEST_TIMEOUT = 10.0

CRED_ENV_VARS = {
    "binance": ("BINANCE_API_KEY", "BINANCE_API_SECRET"),
    "bybit": ("BYBIT_API_KEY", "BYBIT_API_SECRET"),
}


def exchange_base_urls(mode: str) -> dict[str, str]:
    if mode == "live":
        return {"binance": BINANCE_LIVE_URL, "bybit": BYBIT_LIVE_URL}
    return {"binance": BINANCE_TESTNET_URL, "bybit": BYBIT_TESTNET_URL}


def load_credentials(mode: str) -> dict[str, tuple[str, str]]:
    """Read available key/secret pairs from the environment; missing ones absent."""
    creds: dict[str, tuple[str, str]] = {}
    for exchange, (key_var, secret_var) in CRED_ENV_VARS.items():
        key = os.environ.get(key_var)
        secret = os.environ.get(secret_var)
        if key and secret:
            creds[exchange] = (key, secret)
    return creds


class LiveExecutor(BaseExecutor):
    """Places real (testnet or live) LIMIT IOC orders on Binance and Bybit.

    Exchanges without credentials are skipped at construction time and every
    intent touching them is rejected rather than sent. Kraken routes never
    reach this executor; the orchestrator filters them out.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        mode: str,
        credentials: dict[str, tuple[str, str]],
        price_buffer_bps: float = 5.0,
        market_orders: bool = False,
    ) -> None:
        super().__init__()
        if mode not in ("testnet", "live"):
            raise ValueError(f"mode must be testnet or live, got {mode}")
        if mode == "live" and not credentials:
            raise ExecutionError(
                "live mode requires BINANCE_API_KEY/BINANCE_API_SECRET and/or "
                "BYBIT_API_KEY/BYBIT_API_SECRET in the environment"
            )
        self.session = session
        self.mode = mode
        self.credentials = credentials
        self.price_buffer_bps = Decimal(str(price_buffer_bps))
        self.market_orders = market_orders
        self.base_urls = exchange_base_urls(mode)
        self.reports: list[ExecutionReport] = []

    @property
    def name(self) -> str:
        return f"{self.mode}"

    def supports(self, exchange: str) -> bool:
        return exchange in self.credentials and exchange in SUPPORTED_EXCHANGES

    # -- request plumbing -----------------------------------------------------

    async def _post(self, url: str, params: dict[str, str], headers: dict[str, str]) -> dict:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with self.session.post(url, params=params, headers=headers, timeout=timeout) as resp:
            payload: dict = await resp.json()
            if resp.status >= 400:
                detail = payload.get("msg") or payload.get("retMsg") or ""
                raise ExecutionError(f"{self.mode}: HTTP {resp.status} from {url}: {detail}")
        return payload

    async def _get(self, url: str, params: dict[str, str], headers: dict[str, str]) -> dict:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        async with self.session.get(url, params=params, headers=headers, timeout=timeout) as resp:
            payload: dict = await resp.json()
            if resp.status >= 400:
                detail = payload.get("msg") or payload.get("retMsg") or ""
                raise ExecutionError(f"{self.mode}: HTTP {resp.status} from {url}: {detail}")
        return payload

    # -- order construction ------------------------------------------------------

    def _protected_price(self, vwap: Decimal, side: str) -> str:
        """Pad the walked VWAP away from the market: buys up, sells down."""
        buffer = self.price_buffer_bps / Decimal(10_000)
        padded = vwap * (Decimal(1) + buffer) if side == "buy" else vwap * (Decimal(1) - buffer)
        return str(padded.quantize(PRICE_DECIMALS, rounding=ROUND_DOWN).normalize())

    def _amount(self, base: Decimal) -> str:
        return str(base.quantize(AMOUNT_DECIMALS, rounding=ROUND_DOWN).normalize())

    async def _binance_order(
        self, intent: OrderIntent, symbol: str, side: str, base_amount: Decimal, ref_vwap: Decimal
    ) -> tuple[Decimal, Decimal]:
        key, secret = self.credentials["binance"]
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "quantity": self._amount(base_amount),
        }
        if self.market_orders:
            params["type"] = "MARKET"
        else:
            params["type"] = "LIMIT"
            params["timeInForce"] = "IOC"
            params["price"] = self._protected_price(ref_vwap, side)
        signed, headers = binance_signed_request(key, secret, params)
        payload = await self._post(f"{self.base_urls['binance']}/api/v3/order", signed, headers)
        executed = Decimal(str(payload.get("executedQty", "0")))
        spent = Decimal(str(payload.get("cummulativeQuoteQty", "0")))
        if executed <= 0:
            raise ExecutionError(f"binance IOC returned no fill for {symbol} {side}")
        return executed, spent

    async def _bybit_order(
        self, intent: OrderIntent, symbol: str, side: str, base_amount: Decimal, ref_vwap: Decimal
    ) -> tuple[Decimal, Decimal]:
        key, secret = self.credentials["bybit"]
        order_type = "Market" if self.market_orders else "Limit"
        params = {
            "category": "spot",
            "symbol": symbol,
            "side": side.capitalize(),
            "orderType": order_type,
            "qty": self._amount(base_amount),
        }
        if not self.market_orders:
            params["timeInForce"] = "IOC"
            params["price"] = self._protected_price(ref_vwap, side)
        payload_str, headers = bybit_signed_request(key, secret, params)
        url = f"{self.base_urls['bybit']}/v5/order/create?{payload_str}"
        payload = await self._post(url, {}, headers)
        if payload.get("retCode") != 0:
            raise ExecutionError(f"bybit rejected order: {payload.get('retMsg')}")
        executed = Decimal(str(payload.get("result", {}).get("cumulativeExecutedQty", "0")))
        spent = Decimal(str(payload.get("result", {}).get("cumulativeExecutedValue", "0")))
        if executed <= 0:
            raise ExecutionError(f"bybit IOC returned no fill for {symbol} {side}")
        return executed, spent

    async def _place_leg(
        self,
        exchange: str,
        intent: OrderIntent,
        side: str,
        base_amount: Decimal,
        ref_vwap: Decimal,
    ) -> tuple[Decimal, Decimal]:
        symbol = intent.pair.replace("/", "")
        if exchange == "binance":
            return await self._binance_order(intent, symbol, side, base_amount, ref_vwap)
        if exchange == "bybit":
            return await self._bybit_order(intent, symbol, side, base_amount, ref_vwap)
        raise ExecutionError(f"unsupported trading exchange: {exchange} ({KRAKEN_UNSUPPORTED})")

    # -- executor contract ------------------------------------------------------

    async def execute(self, intent: OrderIntent) -> ExecutionReport:
        unsupported = [
            ex for ex in (intent.buy_exchange, intent.sell_exchange) if not self.supports(ex)
        ]
        if unsupported:
            return self._reject(intent, f"no credentials/config for {unsupported[0]}")

        buy_vwap = intent.expected_buy_vwap
        buy_base = intent.budget_quote / buy_vwap
        try:
            executed_buy, spent_buy = await self._place_leg(
                intent.buy_exchange, intent, "buy", buy_base, buy_vwap
            )
            executed_sell, proceeds = await self._place_leg(
                intent.sell_exchange,
                intent,
                "sell",
                executed_buy,
                intent.expected_sell_vwap,
            )
        except (ExecutionError, aiohttp.ClientError, TimeoutError, ValueError) as exc:
            return self._reject(intent, str(exc))

        buy_fill = LegFill(
            side="buy",
            exchange=intent.buy_exchange,
            base_amount=executed_buy,
            vwap_price=spent_buy / executed_buy if executed_buy > 0 else Decimal(0),
            filled_quote=spent_buy,
            fee_quote=Decimal(0),
        )
        sell_fill = LegFill(
            side="sell",
            exchange=intent.sell_exchange,
            base_amount=executed_sell,
            vwap_price=proceeds / executed_sell if executed_sell > 0 else Decimal(0),
            filled_quote=proceeds,
            fee_quote=Decimal(0),
        )
        fully_filled = min(executed_buy, executed_sell) >= buy_base
        report = ExecutionReport(
            intent=intent,
            status=ExecutionStatus.FILLED if fully_filled else ExecutionStatus.PARTIAL,
            mode=self.mode,
            buy_fill=buy_fill,
            sell_fill=sell_fill,
            fees_quote=Decimal(0),
            realized_pnl_quote=pnl_from_legs(buy_fill, sell_fill),
            simulated=False,
        )
        self.reports.append(report)
        return report

    def _reject(self, intent: OrderIntent, reason: str) -> ExecutionReport:
        report = ExecutionReport(
            intent=intent,
            status=ExecutionStatus.REJECTED,
            mode=self.mode,
            simulated=False,
            reason=reason,
        )
        self.reports.append(report)
        return report

    async def balances(self) -> dict[str, dict[str, Decimal]]:
        """Fetch spot balances per credentialed exchange (best effort, keys redacted)."""
        out: dict[str, dict[str, Decimal]] = {}
        if "binance" in self.credentials:
            key, secret = self.credentials["binance"]
            params, headers = binance_signed_request(key, secret, {})
            payload = await self._get(
                f"{self.base_urls['binance']}/api/v3/account", params, headers
            )
            out["binance"] = {
                str(b["asset"]): Decimal(str(b["free"]))
                for b in payload.get("balances", [])
                if Decimal(str(b["free"])) > 0
            }
        if "bybit" in self.credentials:
            key, secret = self.credentials["bybit"]
            payload_str, headers = bybit_signed_request(key, secret, {"accountType": "UNIFIED"})
            payload = await self._get(
                f"{self.base_urls['bybit']}/v5/account/wallet-balance?{payload_str}",
                {},
                headers,
            )
            coins = payload.get("result", {}).get("list", [{}])[0].get("coin", [])
            out["bybit"] = {
                str(c["coin"]): Decimal(str(c.get("walletBalance", "0")))
                for c in coins
                if Decimal(str(c.get("walletBalance", "0"))) > 0
            }
        return out
