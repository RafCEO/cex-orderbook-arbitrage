"""Async SQLite persistence for executed trades (same WAL + batched-insert pattern)."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

from execution.base import ExecutionReport

TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    executed_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    simulated INTEGER NOT NULL,
    route TEXT NOT NULL,
    pair TEXT NOT NULL,
    buy_exchange TEXT NOT NULL,
    sell_exchange TEXT NOT NULL,
    budget_quote REAL NOT NULL,
    buy_base REAL NOT NULL,
    buy_vwap REAL NOT NULL,
    buy_fee REAL NOT NULL,
    sell_base REAL NOT NULL,
    sell_vwap REAL NOT NULL,
    sell_fee REAL NOT NULL,
    fees_quote REAL NOT NULL,
    realized_pnl_quote REAL NOT NULL,
    status TEXT NOT NULL
);
"""

TRADES_INSERT_SQL = (
    "INSERT INTO trades "
    "(executed_at, mode, simulated, route, pair, buy_exchange, sell_exchange, "
    "budget_quote, buy_base, buy_vwap, buy_fee, sell_base, sell_vwap, sell_fee, "
    "fees_quote, realized_pnl_quote, status) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def trade_row(report: ExecutionReport) -> tuple[Any, ...]:
    """Flatten an ExecutionReport into one trades-table row."""
    buy = report.buy_fill
    sell = report.sell_fill
    return (
        report.completed_at.isoformat(),
        report.mode,
        int(report.simulated),
        "cross_exchange",
        report.intent.pair,
        report.intent.buy_exchange,
        report.intent.sell_exchange,
        float(report.intent.budget_quote),
        float(buy.base_amount) if buy else 0.0,
        float(buy.vwap_price) if buy else 0.0,
        float(buy.fee_quote) if buy else 0.0,
        float(sell.base_amount) if sell else 0.0,
        float(sell.vwap_price) if sell else 0.0,
        float(sell.fee_quote) if sell else 0.0,
        float(report.fees_quote),
        float(report.realized_pnl_quote),
        report.status.value,
    )


class TradeLogger:
    """Background drainer for trades: record() enqueues; a worker batches INSERTs."""

    def __init__(self, db_path: str = "data/session.db", batch_size: int = 8) -> None:
        self.db_path = db_path
        self.batch_size = batch_size
        self._queue: asyncio.Queue[ExecutionReport] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._db: aiosqlite.Connection | None = None
        self._running = False

    async def start(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(TRADES_SCHEMA)
        await self._db.commit()
        self._running = True
        self._task = asyncio.create_task(self._drain())

    def record(self, report: ExecutionReport) -> None:
        """Enqueue immediately; never blocks the poll loop."""
        if self._running:
            self._queue.put_nowait(report)

    async def _drain(self) -> None:
        assert self._db is not None
        while self._running:
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                batch: list[ExecutionReport] = [first]
                while len(batch) < self.batch_size and not self._queue.empty():
                    batch.append(self._queue.get_nowait())
                await self._insert_batch(batch)
            except TimeoutError:
                continue

    async def close(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._db is not None and not self._queue.empty():
            await self._insert_batch(self._drain_queue())
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _drain_queue(self) -> list[ExecutionReport]:
        items: list[ExecutionReport] = []
        while not self._queue.empty():
            items.append(self._queue.get_nowait())
        return items

    async def _insert_batch(self, batch: list[ExecutionReport] | None) -> None:
        if not batch or self._db is None:
            return
        await self._db.executemany(TRADES_INSERT_SQL, [trade_row(r) for r in batch])
        await self._db.commit()

    async def trade_stats(self) -> dict[str, Any]:
        """Executed trade count, win rate and realized P&L sums for the session summary."""
        db = self._db
        if db is None:
            return {"count": 0}
        async with db.execute("SELECT COUNT(*) FROM trades") as cur:
            row = await cur.fetchone()
            count = int(row[0]) if row is not None else 0
        if count == 0:
            return {"count": 0}
        async with db.execute("SELECT status, COUNT(*) FROM trades GROUP BY status") as cur:
            by_status = {r[0]: r[1] for r in await cur.fetchall()}
        async with db.execute(
            "SELECT realized_pnl_quote, fees_quote FROM trades WHERE status != 'rejected'"
        ) as cur:
            executed = await cur.fetchall()
        pnl = [Decimal(str(r[0])) for r in executed]
        fees = [Decimal(str(r[1])) for r in executed]
        wins = sum(1 for p in pnl if p > 0)
        executed_count = len(pnl)
        return {
            "count": count,
            "by_status": by_status,
            "executed": executed_count,
            "wins": wins,
            "losses": executed_count - wins,
            "win_rate": wins / executed_count if executed_count else 0.0,
            "realized_pnl": float(sum(pnl, Decimal(0))),
            "total_fees": float(sum(fees, Decimal(0))),
        }

    def export_csv(self, path: str) -> int:
        """Synchronous dump of the trades table to CSV; returns rows written."""
        db = sqlite3.connect(self.db_path)
        try:
            cur = db.execute("SELECT * FROM trades")
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(columns)
                writer.writerows(rows)
            return len(rows)
        finally:
            db.close()
