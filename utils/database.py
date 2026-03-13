"""
utils/database.py
=================
Async SQLite persistence for trade history and equity snapshots.
Uses SQLAlchemy Core with aiosqlite driver.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from sqlalchemy import (
    Column, DateTime, Float, Integer, MetaData, String, Table, Text,
    create_engine, select, insert,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncConnection, create_async_engine

from core.logger import logger
from core.state import TradeRecord, PositionSide


metadata = MetaData()

trades_table = Table(
    "trades",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(20), nullable=False),
    Column("side", String(10), nullable=False),
    Column("entry_price", Float, nullable=False),
    Column("exit_price", Float, nullable=False),
    Column("quantity", Float, nullable=False),
    Column("pnl_usdt", Float, nullable=False),
    Column("pnl_pct", Float, nullable=False),
    Column("fees_usdt", Float, nullable=False),
    Column("exit_reason", String(30), nullable=False),
    Column("opened_at", DateTime, nullable=False),
    Column("closed_at", DateTime, nullable=False),
)

equity_table = Table(
    "equity_curve",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False),
    Column("equity", Float, nullable=False),
)


class Database:
    """Async SQLite wrapper."""

    def __init__(self, db_path: str = "data/trades.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{db_path}"
        self._engine: AsyncEngine = create_async_engine(url, echo=False)

    async def init(self) -> None:
        """Create tables if they don't exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        logger.info("Database initialised")

    async def save_trade(self, trade: TradeRecord) -> None:
        """Persist a closed trade."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(trades_table).values(
                    symbol=trade.symbol,
                    side=trade.side.value,
                    entry_price=trade.entry_price,
                    exit_price=trade.exit_price,
                    quantity=trade.quantity,
                    pnl_usdt=trade.pnl_usdt,
                    pnl_pct=trade.pnl_pct,
                    fees_usdt=trade.fees_usdt,
                    exit_reason=trade.exit_reason,
                    opened_at=trade.opened_at.replace(tzinfo=None),
                    closed_at=trade.closed_at.replace(tzinfo=None),
                )
            )

    async def save_equity(self, ts: datetime, equity: float) -> None:
        """Record equity snapshot."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(equity_table).values(
                    ts=ts.replace(tzinfo=None),
                    equity=equity,
                )
            )

    async def get_trades(self, limit: int = 200) -> List[dict]:
        """Fetch most recent closed trades."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(trades_table).order_by(trades_table.c.closed_at.desc()).limit(limit)
            )
            rows = result.mappings().all()
            return [dict(r) for r in rows]

    async def get_equity_curve(self, limit: int = 2000) -> List[dict]:
        """Fetch equity curve data points."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(equity_table).order_by(equity_table.c.ts.asc()).limit(limit)
            )
            rows = result.mappings().all()
            return [dict(r) for r in rows]

    async def close(self) -> None:
        await self._engine.dispose()
