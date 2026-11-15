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
from connectors.base import BaseConnector, ConnectorError
from connectors.binance import BinanceConnector
from connectors.bybit import BybitConnector
from connectors.kraken import KrakenConnector
from core.models import FeeSchedule, OrderBookSnapshot
from engine.detector import SpreadDetector
from storage.logger import OpportunityLogger

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
}

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
            "Cross-exchange crypto spread monitor and orderbook depth analyzer. "
            "Read-only public-data monitoring; no order execution."
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


def _summary_panel(stats: dict[str, Any], db_path: str) -> Panel:
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
    return Panel(t, title="session summary", border_style="cyan")


async def run(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]
    volume = Decimal(str(args.volume if args.volume is not None else cfg["default_volume_quote"]))
    min_spread = float(args.min_spread if args.min_spread is not None else cfg["min_spread_bps"])
    interval = float(args.interval if args.interval is not None else cfg["poll_interval"])
    duration = float(args.duration)
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

    dashboard = Dashboard(
        connectors=connectors,
        pairs=pairs,
        volume_quote=float(volume),
        min_spread_bps=min_spread,
        interval=interval,
        db_path=db_path,
    )

    live = dashboard.start()
    started = time.monotonic()
    try:
        while duration == 0 or time.monotonic() - started < duration:
            tick_started = time.monotonic()
            snaps = await fetch_all(connectors, pairs)
            opps = detector.evaluate_tick(snaps, volume, fees, triplets)
            for opp in opps:
                opp_logger.log(opp)
            dashboard.update(opps, snaps)
            live.update(dashboard.render())
            elapsed = time.monotonic() - tick_started
            await asyncio.sleep(max(0.0, interval - elapsed))
    except KeyboardInterrupt:
        pass
    finally:
        live.stop()
        await session.close()
        stats = await opp_logger.session_stats()
        await opp_logger.close()
        console.print(_summary_panel(stats, db_path))
        if args.export:
            rows = opp_logger.export_csv(args.export)
            console.print(f"[green]CSV exported to {args.export} ({rows} rows)[/green]")
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
