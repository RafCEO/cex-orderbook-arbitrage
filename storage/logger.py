"""Async SQLite persistence for detected opportunities (WAL, batched inserts)."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import sqlite3
import statistics
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from core.models import ArbitrageOpportunity

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    route TEXT NOT NULL,
    pair TEXT NOT NULL,
    buy_exchange TEXT NOT NULL,
    sell_exchange TEXT NOT NULL,
    volume_quote REAL NOT NULL,
    gross_spread_bps REAL NOT NULL,
    slippage_bps REAL NOT NULL,
    fees_bps REAL NOT NULL,
    net_spread_bps REAL NOT NULL,
    net_delta_quote REAL NOT NULL,
    fill_ratio REAL NOT NULL,
    buy_latency_ms REAL NOT NULL,
    sell_latency_ms REAL NOT NULL,
    suppressed INTEGER NOT NULL DEFAULT 0,
    fx_rate REAL NOT NULL DEFAULT 0,
    fx_quote TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT ''
);
"""

INSERT_SQL = (
    "INSERT INTO opportunities "
    "(detected_at, route, pair, buy_exchange, sell_exchange, volume_quote, "
    "gross_spread_bps, slippage_bps, fees_bps, net_spread_bps, net_delta_quote, "
    "fill_ratio, buy_latency_ms, sell_latency_ms, suppressed, fx_rate, fx_quote, session_id) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def new_session_id() -> str:
    """Fresh run identifier shared by stats scoping, row stamping and exports."""
    return uuid.uuid4().hex


class OpportunityLogger:
    """Background drainer: log() enqueues; a worker task batches INSERTs into SQLite."""

    def __init__(
        self, db_path: str = "data/session.db", batch_size: int = 8, session_id: str | None = None
    ) -> None:
        self.db_path = db_path
        self.batch_size = batch_size
        self.session_id = session_id if session_id is not None else new_session_id()
        self._queue: asyncio.Queue[ArbitrageOpportunity] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._db: aiosqlite.Connection | None = None
        self._running = False

    async def start(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(SCHEMA)
        # dbs created before session scoping lack the column; CREATE IF NOT EXISTS
        # cannot evolve them, so patch via ALTER (duplicate column error is fine).
        with contextlib.suppress(sqlite3.OperationalError):
            await self._db.execute(
                "ALTER TABLE opportunities ADD COLUMN session_id TEXT NOT NULL DEFAULT ''"
            )
        # migrate pre-fx dbs; duplicate-column errors are expected.
        with contextlib.suppress(sqlite3.OperationalError):
            await self._db.execute(
                "ALTER TABLE opportunities ADD COLUMN fx_rate REAL NOT NULL DEFAULT 0"
            )
        with contextlib.suppress(sqlite3.OperationalError):
            await self._db.execute(
                "ALTER TABLE opportunities ADD COLUMN fx_quote TEXT NOT NULL DEFAULT ''"
            )
        await self._db.commit()
        self._running = True
        self._task = asyncio.create_task(self._drain())

    def log(self, opp: ArbitrageOpportunity) -> None:
        """Enqueue immediately; never blocks the poll loop."""
        if self._running:
            self._queue.put_nowait(opp)

    async def _drain(self) -> None:
        assert self._db is not None
        while self._running:
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                batch: list[ArbitrageOpportunity] = [first]
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

    def _drain_queue(self) -> list[ArbitrageOpportunity]:
        items: list[ArbitrageOpportunity] = []
        while not self._queue.empty():
            items.append(self._queue.get_nowait())
        return items

    async def _insert_batch(self, batch: list[ArbitrageOpportunity] | None) -> None:
        if not batch or self._db is None:
            return
        rows = [
            (
                opp.detected_at.isoformat(),
                opp.route.value,
                opp.pair,
                opp.buy_exchange,
                opp.sell_exchange,
                float(opp.volume_quote),
                float(opp.gross_spread_bps),
                float(opp.slippage_bps),
                float(opp.fees_bps),
                float(opp.net_spread_bps),
                float(opp.net_delta_quote),
                float(opp.fill_ratio),
                float(opp.buy_latency_ms),
                float(opp.sell_latency_ms),
                int(opp.suppressed),
                opp.fx_rate,
                opp.fx_quote,
                self.session_id,
            )
            for opp in batch
        ]
        await self._db.executemany(INSERT_SQL, rows)
        await self._db.commit()

    async def session_stats(self) -> dict[str, Any]:
        """Count, mean/median net spread, best row, per-pair counts for THIS run only."""
        db = self._db
        if db is None:
            return {"count": 0}
        async with db.execute(
            "SELECT COUNT(*) FROM opportunities WHERE session_id = ?", (self.session_id,)
        ) as cur:
            row = await cur.fetchone()
            count = int(row[0]) if row is not None else 0
        if count == 0:
            return {"count": 0}
        async with db.execute(
            "SELECT net_spread_bps FROM opportunities WHERE session_id = ?", (self.session_id,)
        ) as cur:
            spreads = [float(r[0]) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT * FROM opportunities WHERE session_id = ? ORDER BY net_spread_bps DESC LIMIT 1",
            (self.session_id,),
        ) as cur:
            best = await cur.fetchone()
        async with db.execute(
            "SELECT pair, COUNT(*) FROM opportunities WHERE session_id = ? GROUP BY pair",
            (self.session_id,),
        ) as cur:
            per_pair = {r[0]: r[1] for r in await cur.fetchall()}
        return {
            "count": count,
            "mean_net_bps": statistics.mean(spreads),
            "median_net_bps": statistics.median(spreads),
            "best": best,
            "per_pair": per_pair,
            "db_path": self.db_path,
            "session_id": self.session_id,
        }

    def export_csv(self, path: str) -> int:
        """Dump only this run's rows to CSV; returns rows written."""
        db = sqlite3.connect(self.db_path)
        try:
            cur = db.execute("SELECT * FROM opportunities WHERE session_id = ?", (self.session_id,))
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
