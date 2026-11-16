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
        assert "session_id" in columns
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
    assert len(insert_cols) == 18


async def test_stats_and_export_exclude_previous_sessions(db_path: str, tmp_path: Path) -> None:
    """Rows stamped with another session id never leak into this run's stats/export."""
    previous = OpportunityLogger(db_path=db_path, session_id="old" * 11)
    await previous.start()
    try:
        previous.log(make_opportunity(net_spread_bps="99"))
        await previous._insert_batch(previous._drain_queue())
    finally:
        await previous.close()

    current = OpportunityLogger(db_path=db_path, session_id="new" * 11)
    await current.start()
    try:
        current.log(make_opportunity(net_spread_bps="4"))
        current.log(make_opportunity(net_spread_bps="10"))
        await current._insert_batch(current._drain_queue())
        stats = await current.session_stats()
        assert stats["count"] == 2
        assert stats["mean_net_bps"] == pytest.approx(7.0)
        assert stats["per_pair"] == {"BTC/USDT": 2}
        assert stats["session_id"] == "new" * 11
    finally:
        await current.close()

    csv_path = str(tmp_path / "current.csv")
    written = current.export_csv(csv_path)
    assert written == 2
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 3  # header + 2 data rows
    assert all(row[rows[0].index("session_id")] == "new" * 11 for row in rows[1:])


async def test_two_loggers_share_db_isolated_stats(db_path: str) -> None:
    """Distinct session ids over one db file keep fully independent stats."""
    first = OpportunityLogger(db_path=db_path, session_id="aaa")
    second = OpportunityLogger(db_path=db_path, session_id="bbb")
    await first.start()
    await second.start()
    try:
        first.log(make_opportunity(net_spread_bps="20"))
        second.log(make_opportunity(net_spread_bps="40"))
        second.log(make_opportunity(net_spread_bps="60"))
        await first._insert_batch(first._drain_queue())
        await second._insert_batch(second._drain_queue())
        assert (await first.session_stats())["count"] == 1
        assert (await second.session_stats())["count"] == 2
    finally:
        await first.close()
        await second.close()


async def test_start_migrates_legacy_db_without_session_column(tmp_path: Path) -> None:
    """Pre-scoping db files get the session_id column added on start."""
    db_path = str(tmp_path / "legacy.db")
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE opportunities ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, detected_at TEXT NOT NULL, "
            "route TEXT NOT NULL, pair TEXT NOT NULL, buy_exchange TEXT NOT NULL, "
            "sell_exchange TEXT NOT NULL, volume_quote REAL NOT NULL, "
            "gross_spread_bps REAL NOT NULL, slippage_bps REAL NOT NULL, "
            "fees_bps REAL NOT NULL, net_spread_bps REAL NOT NULL, "
            "net_delta_quote REAL NOT NULL, fill_ratio REAL NOT NULL, "
            "buy_latency_ms REAL NOT NULL, sell_latency_ms REAL NOT NULL, "
            "suppressed INTEGER NOT NULL DEFAULT 0)"
        )
        # one legacy row from an old run; it must not count toward the new session
        db.execute(
            "INSERT INTO opportunities (detected_at, route, pair, buy_exchange, "
            "sell_exchange, volume_quote, gross_spread_bps, slippage_bps, fees_bps, "
            "net_spread_bps, net_delta_quote, fill_ratio, buy_latency_ms, "
            "sell_latency_ms, suppressed) VALUES ('2026-01-01T00:00:00+00:00', "
            "'cross_exchange', 'BTC/USDT', 'exA', 'exB', 1000.0, 30.0, 5.0, 15.0, "
            "88.0, 1.0, 1.0, 10.0, 12.0, 0)"
        )

    logger = OpportunityLogger(db_path=db_path)
    await logger.start()
    try:
        with sqlite3.connect(db_path) as db:
            columns = [r[1] for r in db.execute("PRAGMA table_info(opportunities)")]
        assert "session_id" in columns
        stats = await logger.session_stats()
        assert stats == {"count": 0}  # legacy row is excluded
    finally:
        await logger.close()


async def test_default_session_ids_are_unique_and_stamped(db_path: str) -> None:
    """Each logger run gets its own uuid hex and stamps inserted rows with it."""
    logger = OpportunityLogger(db_path=db_path)
    await logger.start()
    try:
        logger.log(make_opportunity())
        await logger._insert_batch(logger._drain_queue())
        with sqlite3.connect(db_path) as db:
            stored = db.execute("SELECT session_id FROM opportunities").fetchone()[0]
        assert stored == logger.session_id
    finally:
        await logger.close()
    other = OpportunityLogger(db_path=db_path)
    assert other.session_id != logger.session_id
