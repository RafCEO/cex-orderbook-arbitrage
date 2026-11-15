"""TradeLogger: schema, batched inserts, stats math, CSV export."""

from __future__ import annotations

import csv
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from execution.base import ExecutionReport, ExecutionStatus, LegFill, OrderIntent
from storage.trades_logger import TRADES_INSERT_SQL, TRADES_SCHEMA, TradeLogger, trade_row


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "session.db")


def make_intent(budget: str = "100") -> OrderIntent:
    return OrderIntent(
        pair="BTC/USDT",
        buy_exchange="exA",
        sell_exchange="exB",
        base_asset="BTC",
        quote_asset="USDT",
        budget_quote=Decimal(budget),
        expected_buy_vwap=Decimal("100"),
        expected_sell_vwap=Decimal("101"),
        expected_net_spread_bps=Decimal("10"),
    )


def make_report(
    *,
    status: ExecutionStatus = ExecutionStatus.FILLED,
    pnl: str = "1.5",
    buy_fee: str = "0.1",
    sell_fee: str = "0.1",
    simulated: bool = True,
    mode: str = "paper",
) -> ExecutionReport:
    intent = make_intent()
    buy = LegFill(
        side="buy",
        exchange="exA",
        base_amount=Decimal("1"),
        vwap_price=Decimal("100"),
        filled_quote=Decimal("100"),
        fee_quote=Decimal(buy_fee),
    )
    sell = LegFill(
        side="sell",
        exchange="exB",
        base_amount=Decimal("1"),
        vwap_price=Decimal("101"),
        filled_quote=Decimal("101"),
        fee_quote=Decimal(sell_fee),
    )
    return ExecutionReport(
        intent=intent,
        status=status,
        mode=mode,
        buy_fill=buy,
        sell_fill=sell,
        fees_quote=Decimal(buy_fee) + Decimal(sell_fee),
        realized_pnl_quote=Decimal(pnl),
        simulated=simulated,
    )


async def test_start_creates_trades_table(db_path: str) -> None:
    logger = TradeLogger(db_path=db_path)
    await logger.start()
    try:
        with sqlite3.connect(db_path) as db:
            columns = [r[1] for r in db.execute("PRAGMA table_info(trades)")]
        assert "realized_pnl_quote" in columns
        assert "simulated" in columns
        assert "mode" in columns
    finally:
        await logger.close()


async def test_record_and_flush_round_trip(db_path: str) -> None:
    logger = TradeLogger(db_path=db_path, batch_size=8)
    await logger.start()
    try:
        logger.record(make_report(pnl="2.5"))
        logger.record(make_report(status=ExecutionStatus.PARTIAL, pnl="-0.5"))
        await logger._insert_batch(logger._drain_queue())
        with sqlite3.connect(db_path) as db:
            rows = db.execute(
                "SELECT status, realized_pnl_quote, simulated FROM trades ORDER BY id"
            ).fetchall()
        assert rows[0][0] == "filled"
        assert rows[0][1] == pytest.approx(2.5)
        assert rows[0][2] == 1
        assert rows[1][0] == "partial"
    finally:
        await logger.close()


async def test_close_flushes_pending_queue(db_path: str) -> None:
    logger = TradeLogger(db_path=db_path, batch_size=100)
    await logger.start()
    logger.record(make_report(pnl="1"))
    await logger.close()
    with sqlite3.connect(db_path) as db:
        count = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert count == 1


async def test_trade_stats_math(db_path: str) -> None:
    logger = TradeLogger(db_path=db_path)
    await logger.start()
    try:
        logger.record(make_report(pnl="3"))
        logger.record(make_report(pnl="-1"))
        logger.record(make_report(status=ExecutionStatus.REJECTED, pnl="0"))
        await logger._insert_batch(logger._drain_queue())
        stats = await logger.trade_stats()
        assert stats["count"] == 3
        assert stats["executed"] == 2
        assert stats["wins"] == 1
        assert stats["losses"] == 1
        assert stats["win_rate"] == pytest.approx(0.5)
        assert stats["realized_pnl"] == pytest.approx(2.0)
        assert stats["total_fees"] == pytest.approx(0.4)
    finally:
        await logger.close()


async def test_trade_stats_empty(db_path: str) -> None:
    logger = TradeLogger(db_path=db_path)
    await logger.start()
    try:
        assert await logger.trade_stats() == {"count": 0}
    finally:
        await logger.close()


async def test_export_csv_writes_header_and_rows(db_path: str, tmp_path: Path) -> None:
    logger = TradeLogger(db_path=db_path)
    await logger.start()
    try:
        logger.record(make_report(pnl="1.25"))
        await logger._insert_batch(logger._drain_queue())
    finally:
        await logger.close()
    csv_path = str(tmp_path / "trades.csv")
    written = logger.export_csv(csv_path)
    assert written == 1
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][0] == "id"
    header = rows[0]
    assert rows[1][header.index("realized_pnl_quote")] == "1.25"
    assert rows[1][header.index("mode")] == "paper"


def test_trade_row_flattens_report_with_missing_legs() -> None:
    intent = make_intent()
    rejected = ExecutionReport(
        intent=intent, status=ExecutionStatus.REJECTED, mode="paper", simulated=True
    )
    row = trade_row(rejected)
    assert row[16] == "rejected"
    assert row[8] == 0.0  # buy_base zero when leg absent


def test_insert_sql_columns_align_with_schema() -> None:
    columns = [
        line.split()[0].strip()
        for line in TRADES_SCHEMA.splitlines()
        if line.strip() and not line.strip().startswith(("CREATE", ")", "id"))
    ]
    insert_cols = TRADES_INSERT_SQL.split("(")[1].split(")")[0].replace(" ", "").split(",")
    assert insert_cols == columns
    assert len(insert_cols) == 17
