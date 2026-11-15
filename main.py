"""cex-orderbook-arbitrage entrypoint: config, orchestrator loop, shutdown summary."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cli.dashboard import Dashboard
from cli.trading_panel import TradingPanel
from connectors.base import BaseConnector, ConnectorError
from connectors.binance import BinanceConnector
from connectors.bybit import BybitConnector
from connectors.kraken import KrakenConnector
from core.models import ArbitrageOpportunity, FeeSchedule, OrderBookSnapshot, RouteType
from engine.detector import SpreadDetector
from execution.base import BaseExecutor, ExecutionReport, build_intent
from execution.live_executor import LiveExecutor, load_credentials
from execution.paper_executor import PaperExecutor
from execution.portfolio import Portfolio
from execution.risk import RiskConfig, RiskGuard
from storage.logger import OpportunityLogger
from storage.trades_logger import TradeLogger

BUILTIN_DEFAULTS: dict[str, Any] = {
    "poll_interval": 2.0,
    "default_volume_quote": 1000.0,
    "min_spread_bps": 5.0,
    "depth_levels": 50,
    "max_snapshot_age_ms": 5000.0,
    "db_path": "data/session.db",
    "spike_filter": {"window": 20, "deviation_bps": 25.0, "confirmations": 2},
    "triangular_triplets": [["BTC/USDT", "ETH/BTC", "ETH/USDT"]],
    "exchanges": {
        "binance": {
            "enabled": True,
            "maker_fee_bps": 10.0,
            "taker_fee_bps": 10.0,
            "rate_limit_per_sec": 10.0,
        },
        "kraken": {
            "enabled": True,
            "maker_fee_bps": 16.0,
            "taker_fee_bps": 26.0,
            "rate_limit_per_sec": 1.0,
        },
        "bybit": {
            "enabled": True,
            "maker_fee_bps": 10.0,
            "taker_fee_bps": 10.0,
            "rate_limit_per_sec": 5.0,
        },
    },
    "trading": {
        "mode": "monitor",
        "starting_balances": {"binance": 1000.0, "kraken": 1000.0, "bybit": 1000.0},
        "max_trade_quote": 100.0,
        "max_exposure_quote": 500.0,
        "daily_loss_limit_quote": 50.0,
        "min_act_spread_bps": 5.0,
        "market_orders": False,
        "price_buffer_bps": 5.0,
    },
}

MODES = ("monitor", "paper", "testnet", "live")
TRADING_MODES = ("paper", "testnet", "live")

CONNECTOR_CLASSES: dict[str, type[BaseConnector]] = {
    "binance": BinanceConnector,
    "kraken": KrakenConnector,
    "bybit": BybitConnector,
}

console = Console()
log = logging.getLogger("cex-orderbook-arbitrage")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str) -> dict[str, Any]:
    cfg_file = Path(path)
    if cfg_file.exists():
        with open(cfg_file, encoding="utf-8") as fh:
            file_cfg = yaml.safe_load(fh) or {}
        return deep_merge(BUILTIN_DEFAULTS, file_cfg)
    return dict(BUILTIN_DEFAULTS)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cex-orderbook-arbitrage",
        description=(
            "Cross-exchange crypto spread monitor, orderbook depth analyzer and "
            "arbitrage trading bot. Monitor mode is read-only public-data polling; "
            "paper/testnet/live add simulated, testnet or real order execution."
        ),
    )
    parser.add_argument(
        "--pairs",
        default="BTC/USDT,ETH/USDT",
        help="comma-separated canonical pairs, e.g. BTC/USDT,ETH/USDT",
    )
    parser.add_argument(
        "--volume",
        type=float,
        default=None,
        help="trade size in quote currency (default from config)",
    )
    parser.add_argument(
        "--min-spread",
        type=float,
        default=None,
        help="minimum net spread in bps (default from config)",
    )
    parser.add_argument(
        "--export",
        default=None,
        metavar="PATH.csv",
        help="dump session table to CSV at exit",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="poll interval seconds (default from config)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="run for N seconds, 0 runs until Ctrl+C",
    )
    parser.add_argument("--config", default="config.yaml", help="config file path")
    parser.add_argument(
        "--exchanges",
        default=None,
        help="comma-separated exchange subset, e.g. binance,bybit",
    )
    parser.add_argument(
        "--mode",
        choices=MODES,
        default=None,
        help="operating mode: monitor (default, read-only), paper (simulated fills), "
        "testnet or live (real orders on Binance/Bybit)",
    )
    parser.add_argument(
        "--ack-live-risk",
        action="store_true",
        help="required acknowledgement to run live mode against real funds",
    )
    return parser.parse_args(argv)


def build_fee_schedules(cfg: dict[str, Any]) -> dict[str, FeeSchedule]:
    out: dict[str, FeeSchedule] = {}
    for name, ecfg in cfg["exchanges"].items():
        if isinstance(ecfg, dict):
            out[name] = FeeSchedule(
                maker_bps=Decimal(str(ecfg.get("maker_fee_bps", 0))),
                taker_bps=Decimal(str(ecfg.get("taker_fee_bps", 0))),
            )
    return out


def enabled_exchanges(cfg: dict[str, Any]) -> list[str]:
    return [
        name
        for name, ecfg in cfg["exchanges"].items()
        if isinstance(ecfg, dict) and ecfg.get("enabled", True)
    ]


LIVE_RISK_WARNING = (
    "[bold red]LIVE TRADING MODE[/bold red]\n"
    "Real orders with real funds will be placed on Binance and Bybit.\n"
    "Polling-latency arbitrage on public REST data typically has NEGATIVE net "
    "edge after fees; expect losses at this data granularity.\n"
    "You are solely responsible for all trades, losses and account risk."
)


def check_live_gate(mode: str, args: argparse.Namespace) -> str | None:
    """Return a refusal reason when live mode lacks credentials or acknowledgement."""
    if mode != "live":
        return None
    if not args.ack_live_risk:
        return (
            "live mode requires --ack-live-risk: confirm you understand this "
            "places real orders with real funds and may lose money"
        )
    creds = load_credentials(mode)
    if not creds:
        return (
            "live mode requires BINANCE_API_KEY/BINANCE_API_SECRET and/or "
            "BYBIT_API_KEY/BYBIT_API_SECRET in the environment"
        )
    return None


def resolve_mode(args: argparse.Namespace, cfg: dict[str, Any]) -> str:
    mode = args.mode if args.mode is not None else cfg.get("trading", {}).get("mode", "monitor")
    return str(mode)


def pair_assets(pair: str) -> tuple[str, str]:
    base, quote = pair.split("/")
    return base, quote


def starting_portfolio(cfg: dict[str, Any], exchanges: list[str]) -> Portfolio:
    balances = cfg.get("trading", {}).get("starting_balances", {})
    starting = {name: Decimal(str(balances.get(name, 1000))) for name in exchanges}
    return Portfolio(starting_quote=starting)


def build_executor(
    mode: str,
    cfg: dict[str, Any],
    session: aiohttp.ClientSession,
    portfolio: Portfolio,
    fees: dict[str, FeeSchedule],
    exchanges: list[str],
) -> tuple[BaseExecutor | None, dict[str, str]]:
    """Build the executor for the mode; warnings returned for degraded setups."""
    warnings: dict[str, str] = {}
    if mode == "paper":
        return PaperExecutor(portfolio=portfolio, fees=fees), warnings
    if mode in ("testnet", "live"):
        creds = load_credentials(mode)
        tcfg = cfg.get("trading", {})
        executor = LiveExecutor(
            session=session,
            mode=mode,
            credentials=creds,
            price_buffer_bps=float(tcfg.get("price_buffer_bps", 5.0)),
            market_orders=bool(tcfg.get("market_orders", False)),
        )
        if not creds:
            warnings["*"] = (
                "no exchange credentials in environment: testnet degrades to monitor-only"
                if mode == "testnet"
                else "no exchange credentials in environment"
            )
        for exchange in ("binance", "bybit"):
            if exchange not in creds and exchange in exchanges and creds:
                warnings[exchange] = (
                    f"{exchange}: no API credentials, routes involving it degrade to monitor-only"
                )
        return executor, warnings
    return None, warnings


async def execute_opportunities(
    opps: list[ArbitrageOpportunity],
    snaps: dict[tuple[str, str], OrderBookSnapshot],
    executor: BaseExecutor,
    risk: RiskGuard,
    portfolio: Portfolio,
    trade_logger: TradeLogger,
) -> list[ExecutionReport]:
    """Run detector-confirmed cross-exchange opportunities through risk gate and executor.

    Triangular routes are monitor-only: their composite pair has no single
    orderbook pair to execute against.
    """
    reports: list[ExecutionReport] = []
    for opp in opps:
        if opp.suppressed or opp.route != RouteType.CROSS_EXCHANGE:
            continue
        try:
            base_asset, quote_asset = pair_assets(opp.pair)
        except ValueError:
            continue
        buy_snap = snaps.get((opp.buy_exchange, opp.pair))
        sell_snap = snaps.get((opp.sell_exchange, opp.pair))
        if buy_snap is None or sell_snap is None:
            continue
        intent = build_intent(opp, buy_snap, sell_snap, base_asset, quote_asset)
        if intent is None:
            continue
        rejection = risk.check(intent, portfolio.open_inventory_cost())
        if rejection is not None:
            continue
        report = await executor.execute(intent)
        reports.append(report)
        if report.status.value != "rejected":
            risk.record_pnl(report.realized_pnl_quote)
            trade_logger.record(report)
    return reports


async def fetch_all(
    connectors: dict[str, BaseConnector],
    pairs: list[str],
) -> dict[tuple[str, str], OrderBookSnapshot]:
    snaps: dict[tuple[str, str], OrderBookSnapshot] = {}
    results = await asyncio.gather(
        *(c.fetch_orderbook(pair) for c in connectors.values() for pair in pairs),
        return_exceptions=True,
    )
    idx = 0
    for connector in connectors.values():
        for pair in pairs:
            result = results[idx]
            idx += 1
            if isinstance(result, BaseException):
                if not isinstance(result, ConnectorError):
                    connector.consecutive_failures += 1
                connector.last_error = str(result)
                log.warning("fetch failed %s %s: %s", connector.exchange_name, pair, result)
            else:
                snaps[(connector.exchange_name, pair)] = result
    return snaps


def _summary_panel(
    stats: dict[str, Any], db_path: str, trade_stats: dict[str, Any] | None = None
) -> Panel:
    t = Table(show_header=False, box=None)
    t.add_column(style="bold cyan")
    t.add_column()
    t.add_row("rows", str(stats.get("count", 0)))
    if stats.get("count"):
        t.add_row("mean net bps", f"{stats['mean_net_bps']:.2f}")
        t.add_row("median net bps", f"{stats['median_net_bps']:.2f}")
        best = stats.get("best")
        if best:
            t.add_row("best route", f"{best[3]} {best[4]}->{best[5]} net {best[10]:.2f} bps")
        per_pair = stats.get("per_pair", {})
        if per_pair:
            t.add_row("per pair", ", ".join(f"{k}: {v}" for k, v in per_pair.items()))
    t.add_row("db", db_path)
    if trade_stats and trade_stats.get("count"):
        t.add_row(
            "trades",
            f"{trade_stats['executed']} executed / {trade_stats['count']} logged "
            f"(win rate {trade_stats['win_rate'] * 100:.0f}%)",
        )
        t.add_row("realized pnl", f"{trade_stats['realized_pnl']:.4f}")
        t.add_row("fees paid", f"{trade_stats['total_fees']:.4f}")
    return Panel(t, title="session summary", border_style="cyan")


async def run(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    volume = Decimal(str(args.volume if args.volume is not None else cfg["default_volume_quote"]))
    min_spread = float(args.min_spread if args.min_spread is not None else cfg["min_spread_bps"])
    interval = float(args.interval if args.interval is not None else cfg["poll_interval"])
    duration = float(args.duration)
    mode = resolve_mode(args, cfg)
    gate_refusal = check_live_gate(mode, args)
    if gate_refusal is not None:
        console.print(f"[red]refusing to start: {gate_refusal}[/red]")
        return 2
    if args.exchanges:
        requested = [e.strip() for e in args.exchanges.split(",")]
    else:
        requested = enabled_exchanges(cfg)
    enabled = [e for e in requested if e in CONNECTOR_CLASSES]
    if not enabled:
        console.print("[red]no enabled exchanges; check --exchanges / config[/red]")
        return 1
    triplets = [tuple(t) for t in cfg["triangular_triplets"]]
    spike = cfg["spike_filter"]

    detector = SpreadDetector(
        min_spread_bps=min_spread,
        max_snapshot_age_ms=float(cfg["max_snapshot_age_ms"]),
        spike_window=int(spike["window"]),
        spike_deviation_bps=float(spike["deviation_bps"]),
        confirmations=int(spike["confirmations"]),
    )
    fees = build_fee_schedules(cfg)

    db_path = str(cfg["db_path"])
    opp_logger = OpportunityLogger(db_path=db_path)
    await opp_logger.start()
    trade_logger = TradeLogger(db_path=db_path)
    await trade_logger.start()

    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=30),
        headers={"User-Agent": "cex-orderbook-arbitrage/1.0"},
    )
    connectors: dict[str, BaseConnector] = {}
    for name in enabled:
        rate = cfg["exchanges"].get(name, {}).get("rate_limit_per_sec")
        connectors[name] = CONNECTOR_CLASSES[name](
            session=session,
            depth=int(cfg["depth_levels"]),
            rate_limit_per_sec=float(rate) if rate else None,
        )

    trading = mode in TRADING_MODES
    portfolio: Portfolio | None = None
    risk: RiskGuard | None = None
    executor: BaseExecutor | None = None
    executor_warnings: dict[str, str] = {}
    if trading:
        portfolio = starting_portfolio(cfg, enabled)
        risk_cfg = RiskConfig.from_config(cfg.get("trading", {}))
        if args.min_spread is not None:
            risk_cfg.min_act_spread_bps = Decimal(str(args.min_spread))
        risk = RiskGuard(risk_cfg)
        executor, executor_warnings = build_executor(mode, cfg, session, portfolio, fees, enabled)
        if mode == "live":
            console.print(Panel(LIVE_RISK_WARNING, border_style="red"))
        for warning in executor_warnings.values():
            console.print(f"[yellow]warning: {warning}[/yellow]")

    dashboard = Dashboard(
        connectors=connectors,
        pairs=pairs,
        volume_quote=float(volume),
        min_spread_bps=min_spread,
        interval=interval,
        db_path=db_path,
    )
    trading_panel = TradingPanel()

    live = dashboard.start()
    started = time.monotonic()
    try:
        while duration == 0 or time.monotonic() - started < duration:
            tick_started = time.monotonic()
            snaps = await fetch_all(connectors, pairs)
            opps = detector.evaluate_tick(snaps, volume, fees, triplets)
            for opp in opps:
                opp_logger.log(opp)
            reports: list[ExecutionReport] = []
            if trading and executor is not None and risk is not None and portfolio is not None:
                executor.books = snaps
                reports = await execute_opportunities(
                    opps, snaps, executor, risk, portfolio, trade_logger
                )
            mids = {
                key: snap.mid_price for key, snap in snaps.items() if snap.mid_price is not None
            }
            dashboard.update(opps, snaps)
            trading_panel.update(
                mode if trading else "monitor",
                portfolio,
                reports,
                halted=risk.halted if risk is not None else False,
                halt_reason=risk.halt_reason if risk is not None else None,
                mids=mids,
            )
            live.update(dashboard.render(trading_panel.render() if trading else None))
            elapsed = time.monotonic() - tick_started
            await asyncio.sleep(max(0.0, interval - elapsed))
    except KeyboardInterrupt:
        pass
    finally:
        live.stop()
        await session.close()
        stats = await opp_logger.session_stats()
        trade_stats = await trade_logger.trade_stats() if trading else None
        await opp_logger.close()
        await trade_logger.close()
        console.print(_summary_panel(stats, db_path, trade_stats))
        if args.export:
            rows = opp_logger.export_csv(args.export)
            console.print(f"[green]CSV exported to {args.export} ({rows} rows)[/green]")
            if trading:
                stem = Path(args.export)
                trades_csv = str(stem.with_name(f"trades_{stem.name}"))
                trade_rows = trade_logger.export_csv(trades_csv)
                console.print(
                    f"[green]trades CSV exported to {trades_csv} ({trade_rows} rows)[/green]"
                )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    args = parse_args(argv)
    cfg = load_config(args.config)
    try:
        return asyncio.run(run(args, cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
