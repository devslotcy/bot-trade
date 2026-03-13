"""
core/state.py
=============
Shared, thread-safe bot state (positions, equity, flags).
Uses asyncio.Lock so async tasks access it safely.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional


class BotStatus(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    CIRCUIT_BREAKER = "circuit_breaker"
    DAILY_LIMIT_HIT = "daily_limit_hit"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class Position:
    """Represents one open position."""

    symbol: str
    side: PositionSide
    entry_price: float
    quantity: float                  # base asset amount
    stop_loss: float
    take_profit: float
    atr_at_entry: float
    partial_tp_price: float
    partial_tp_done: bool = False
    trailing_stop_active: bool = False
    trailing_stop_price: float = 0.0
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    entry_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    risk_amount: float = 0.0         # USDT risked on this trade

    @property
    def age_hours(self) -> float:
        delta = datetime.now(timezone.utc) - self.opened_at
        return delta.total_seconds() / 3600

    @property
    def unrealised_pnl(self, current_price: float = 0.0) -> float:
        """Approximate PnL — pass current_price externally."""
        if self.side == PositionSide.LONG:
            return (current_price - self.entry_price) * self.quantity
        return (self.entry_price - current_price) * self.quantity


@dataclass
class TradeRecord:
    """Closed trade result stored in history."""

    symbol: str
    side: PositionSide
    entry_price: float
    exit_price: float
    quantity: float
    pnl_usdt: float
    pnl_pct: float
    opened_at: datetime
    closed_at: datetime
    exit_reason: str          # "TP" | "SL" | "TRAILING" | "PARTIAL_TP" | "TIME_EXIT" | "MANUAL"
    fees_usdt: float = 0.0


@dataclass
class DailyStats:
    """Resets at UTC midnight."""

    date: str = ""
    starting_equity: float = 0.0
    realized_pnl: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0

    @property
    def loss_pct(self) -> float:
        if self.starting_equity == 0:
            return 0.0
        return self.realized_pnl / self.starting_equity

    @property
    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return self.win_count / total if total else 0.0


class BotState:
    """
    Central shared state for the bot.

    All mutations go through async methods that acquire the internal lock,
    so multiple coroutines never corrupt the state simultaneously.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.status: BotStatus = BotStatus.STOPPED
        self.positions: Dict[str, Position] = {}   # symbol → Position
        self.trade_history: List[TradeRecord] = []
        self.equity: float = 0.0
        self.daily: DailyStats = DailyStats()
        self.circuit_breaker_failures: int = 0
        self.circuit_breaker_until: Optional[datetime] = None
        self.last_loss_at: Optional[datetime] = None
        self.equity_curve: List[Dict] = []         # {ts, equity} snapshots

    # ── Status helpers ────────────────────────────────────────────────────────

    async def set_status(self, status: BotStatus) -> None:
        async with self._lock:
            self.status = status

    @property
    def is_running(self) -> bool:
        return self.status == BotStatus.RUNNING

    # ── Positions ─────────────────────────────────────────────────────────────

    async def open_position(self, pos: Position) -> None:
        async with self._lock:
            self.positions[pos.symbol] = pos

    async def close_position(self, symbol: str) -> Optional[Position]:
        async with self._lock:
            return self.positions.pop(symbol, None)

    async def update_position(self, symbol: str, **kwargs) -> None:
        async with self._lock:
            if symbol in self.positions:
                pos = self.positions[symbol]
                for k, v in kwargs.items():
                    setattr(pos, k, v)

    @property
    def open_count(self) -> int:
        return len(self.positions)

    # ── Trade history ─────────────────────────────────────────────────────────

    async def record_trade(self, trade: TradeRecord) -> None:
        async with self._lock:
            self.trade_history.append(trade)
            self.daily.realized_pnl += trade.pnl_usdt
            self.daily.trade_count += 1
            if trade.pnl_usdt >= 0:
                self.daily.win_count += 1
            else:
                self.daily.loss_count += 1
                self.last_loss_at = datetime.now(timezone.utc)

    # ── Equity ────────────────────────────────────────────────────────────────

    async def update_equity(self, equity: float) -> None:
        async with self._lock:
            self.equity = equity
            self.equity_curve.append(
                {"ts": datetime.now(timezone.utc).isoformat(), "equity": equity}
            )

    # ── Daily reset ───────────────────────────────────────────────────────────

    async def reset_daily(self, current_equity: float) -> None:
        async with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.daily = DailyStats(
                date=today,
                starting_equity=current_equity,
            )

    # ── Circuit breaker ───────────────────────────────────────────────────────

    async def increment_failures(self, pause_minutes: int) -> int:
        async with self._lock:
            self.circuit_breaker_failures += 1
            if self.circuit_breaker_failures >= 5:
                from datetime import timedelta
                self.circuit_breaker_until = datetime.now(timezone.utc) + timedelta(
                    minutes=pause_minutes
                )
                self.status = BotStatus.CIRCUIT_BREAKER
            return self.circuit_breaker_failures

    async def reset_failures(self) -> None:
        async with self._lock:
            self.circuit_breaker_failures = 0
            self.circuit_breaker_until = None

    def circuit_breaker_active(self) -> bool:
        if self.circuit_breaker_until is None:
            return False
        return datetime.now(timezone.utc) < self.circuit_breaker_until
