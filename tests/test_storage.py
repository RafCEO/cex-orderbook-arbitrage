"""OpportunityLogger: batched inserts, CSV round-trip, session stats math."""

from __future__ import annotations

import asyncio
import csv
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from storage.logger import INSERT_SQL, SCHEMA, OpportunityLogger
from tests.fakes import make_opportunity


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "session.db")


async def test_start_creates_schema_and_directories(tmp_path: Path) -> None:
    db_path = str(tmp_path / "nested" / "dir" / "session.db")
    logger = OpportunityLogger(db_path=db_path)
    await logger.start()
    try:
        assert Path(db_path).exists()
        with sqlite3.connect(db_path) as db:
            columns = [r[1] for r in db.execute("PRAGMA table_info(opportunities)")]
        assert "net_spread_bps" in columns
        assert "suppressed" in columns
    finally:
        await logger.close()


async def test_batched_inserts_flush_to_sqlite(db_path: str) -> None:
    logger = OpportunityLogger(db_path=db_path, batch_size=3)
    await logger.start()
    try:
        for i in range(7):
            logger.log(make_opportunity(net_spread_bps=str(10 + i)))
        await logger._insert_batch(None)  # no-op guard
        # drain the queue through the same path the worker uses
        await logger._insert_batch(logger._drain_queue())
        with sqlite3.connect(db_path) as db:
            count = db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        assert count == 7
    finally:
        await logger.close()


async def test_close_flushes_pending_queue(db_path: str) -> None:
    logger = OpportunityLogger(db_path=db_path, batch_size=100)
    await logger.start()
    logger.log(make_opportunity(net_spread_bps="11"))
    logger.log(make_opportunity(net_spread_bps="22", pair="ETH/USDT"))
    await logger.close()
    with sqlite3.connect(db_path) as db:
        rows = db.execute("SELECT pair FROM opportunities ORDER BY id").fetchall()
    assert rows == [("BTC/USDT",), ("ETH/USDT",)]


async def test_export_csv_round_trip_all_rows_with_header(db_path: str, tmp_path: Path) -> None:
    logger = OpportunityLogger(db_path=db_path, batch_size=8)
    await logger.start()
    try:
        logger.log(make_opportunity(net_spread_bps="10.5"))
        logger.log(make_opportunity(net_spread_bps="-2.5", pair="ETH/USDT", suppressed=True))
        await logger._insert_batch(logger._drain_queue())
    finally:
        await logger.close()

    csv_path = str(tmp_path / "exports" / "session.csv")
    written = logger.export_csv(csv_path)
    assert written == 2

    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 3  # header + 2 data rows
    header, first, second = rows
    assert header[0] == "id"
    assert "net_spread_bps" in header
    assert "pair" in header
    assert first[header.index("pair")] == "BTC/USDT"
    assert Decimal(first[header.index("net_spread_bps")]) == Decimal("10.5")
    assert second[header.index("pair")] == "ETH/USDT"
    assert second[header.index("suppressed")] == "1"


async def test_session_stats_math(db_path: str) -> None:
    logger = OpportunityLogger(db_path=db_path, batch_size=8)
    await logger.start()
    try:
        for value in ("4", "10", "16"):
            logger.log(make_opportunity(net_spread_bps=value))
        await logger._insert_batch(logger._drain_queue())
        stats = await logger.session_stats()
        assert stats["count"] == 3
        assert stats["mean_net_bps"] == pytest.approx(10.0)
        assert stats["median_net_bps"] == pytest.approx(10.0)
        assert stats["per_pair"] == {"BTC/USDT": 3}
        assert stats["db_path"] == db_path
        best = stats["best"]
        assert best is not None
        assert float(best[10]) == pytest.approx(16.0)  # net_spread_bps column
    finally:
        await logger.close()


async def test_session_stats_empty_database(db_path: str) -> None:
    logger = OpportunityLogger(db_path=db_path)
    await logger.start()
    try:
        stats = await logger.session_stats()
        assert stats == {"count": 0}
    finally:
        await logger.close()


async def test_log_before_start_is_dropped(db_path: str) -> None:
    logger = OpportunityLogger(db_path=db_path)
    logger.log(make_opportunity())
    assert logger._queue.empty()
    stats = await logger.session_stats()
    assert stats == {"count": 0}


async def test_drain_loop_batches_inserts(db_path: str) -> None:
    logger = OpportunityLogger(db_path=db_path, batch_size=3)
    await logger.start()
    try:
        for i in range(4):
            logger.log(make_opportunity(net_spread_bps=str(i)))
        # background worker picks the first item within its 1s poll window
        await asyncio.sleep(0.05)
        with sqlite3.connect(db_path) as db:
            count = db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        assert count == 4  # batch drains the remaining queue items in one insert
    finally:
        await logger.close()


def test_insert_sql_columns_align_with_schema() -> None:
    columns = [
        line.split()[0].strip()
        for line in SCHEMA.splitlines()
        if line.strip() and not line.strip().startswith(("CREATE", ")", "id"))
    ]
    insert_cols = INSERT_SQL.split("(")[1].split(")")[0].replace(" ", "").split(",")
    assert insert_cols == columns
    assert len(insert_cols) == 15
