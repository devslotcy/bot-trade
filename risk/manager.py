"""
risk/manager.py
===============
Risk management layer.

Responsibilities:
  • Pre-trade checks (daily loss limit, cooldown, max positions, circuit-breaker)
  • Position sizing  (equity × risk% / SL distance)
  • SL / TP / trailing-stop price computation
  • Slippage guard
  • Post-trade state updates

All sizing is denominated in USDT (base quote for BTCUSDT/ETHUSDT pairs).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from core.config import BotConfig
from core.logger import logger
from core.state import BotState, BotStatus, Position, PositionSide


@dataclass
class TradeSetup:
    """Computed trade parameters ready for execution."""

    symbol: str
    side: PositionSide
    entry_price: float
    stop_loss: float
    take_profit: float
    partial_tp_price: float
    quantity: float          # base asset
    risk_usdt: float
    atr: float
    r_distance: float        # price distance for 1R


class RiskManager:
    """
    Validates and sizes every trade before execution.

    Raises ValueError with descriptive message if any check fails
    so the caller can log/alert and skip.
    """

    def __init__(self, config: BotConfig, state: BotState) -> None:
        self._cfg = config
        self._risk = config.risk
        self._state = state

    # ── Pre-trade gate ────────────────────────────────────────────────────────

    def check_pre_trade(self, symbol: str) -> None:
        """
        Run all pre-trade checks. Raises ValueError if any check fails.

        Args:
            symbol: The symbol about to be traded.
        """
        # 1. Bot must be running
        if not self._state.is_running:
            raise ValueError(f"Bot not running (status={self._state.status})")

        # 2. Circuit breaker
        if self._state.circuit_breaker_active():
            until = self._state.circuit_breaker_until
            raise ValueError(f"Circuit breaker active until {until}")

        # 3. Daily loss limit
        if self._state.daily.loss_pct <= self._risk.daily_loss_limit:
            raise ValueError(
                f"Daily loss limit hit ({self._state.daily.loss_pct:.2%} ≤ "
                f"{self._risk.daily_loss_limit:.2%})"
            )

        # 4. Max concurrent positions
        if self._state.open_count >= self._risk.max_concurrent_positions:
            raise ValueError(
                f"Max concurrent positions reached ({self._state.open_count}/"
                f"{self._risk.max_concurrent_positions})"
            )

        # 5. No duplicate position for same symbol
        if symbol in self._state.positions:
            raise ValueError(f"Already have open position for {symbol}")

        # 6. Loss cooldown
        if self._state.last_loss_at is not None:
            cooldown_end = self._state.last_loss_at + timedelta(
                hours=self._risk.loss_cooldown_hours
            )
            now = datetime.now(timezone.utc)
            if now < cooldown_end:
                remaining = (cooldown_end - now).seconds // 60
                raise ValueError(f"Loss cooldown active — {remaining}m remaining")

    # ── Sizing ────────────────────────────────────────────────────────────────

    def compute_setup(
        self,
        symbol: str,
        side: PositionSide,
        entry_price: float,
        atr: float,
        symbol_info: dict,
    ) -> TradeSetup:
        """
        Compute SL, TP, partial TP, and position size.

        Args:
            symbol:       Trading pair.
            side:         LONG or SHORT.
            entry_price:  Proposed entry price.
            atr:          ATR at signal time.
            symbol_info:  Binance symbol info dict for lot-size rounding.

        Returns:
            TradeSetup with all parameters.

        Raises:
            ValueError: If resulting quantity is below minimum notional.
        """
        sl_dist = self._risk.sl_atr_multiplier * atr

        if side == PositionSide.LONG:
            stop_loss = entry_price - sl_dist
            take_profit = entry_price + self._risk.tp_r_ratio * sl_dist
            partial_tp = entry_price + self._risk.partial_tp_r * sl_dist
        else:
            stop_loss = entry_price + sl_dist
            take_profit = entry_price - self._risk.tp_r_ratio * sl_dist
            partial_tp = entry_price - self._risk.partial_tp_r * sl_dist

        equity = self._state.equity
        risk_usdt = equity * self._risk.risk_per_trade
        quantity_raw = risk_usdt / sl_dist

        # Round down to exchange lot-size precision
        quantity = self._round_quantity(quantity_raw, symbol_info)

        # Check minimum notional
        min_notional = self._get_min_notional(symbol_info)
        notional = quantity * entry_price
        if notional < min_notional:
            raise ValueError(
                f"Notional {notional:.2f} USDT below minimum {min_notional:.2f} USDT"
            )

        logger.debug(
            f"Trade setup {symbol} {side.value}: entry={entry_price:.4f} "
            f"SL={stop_loss:.4f} TP={take_profit:.4f} qty={quantity:.6f} "
            f"risk={risk_usdt:.2f} USDT"
        )

        return TradeSetup(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            partial_tp_price=round(partial_tp, 8),
            quantity=quantity,
            risk_usdt=risk_usdt,
            atr=atr,
            r_distance=sl_dist,
        )

    # ── Slippage guard ────────────────────────────────────────────────────────

    def check_slippage(self, expected: float, actual: float) -> bool:
        """
        Return True if slippage is within acceptable bounds.

        Args:
            expected: Signal/limit price.
            actual:   Fill price from exchange.
        """
        if expected == 0:
            return False
        slippage = abs(actual - expected) / expected
        if slippage > self._risk.slippage_guard_pct:
            logger.warning(
                f"Slippage too high: {slippage:.4%} > {self._risk.slippage_guard_pct:.4%}"
            )
            return False
        return True

    # ── Exit logic ────────────────────────────────────────────────────────────

    def evaluate_exit(
        self,
        pos: Position,
        current_price: float,
    ) -> Tuple[bool, str, float]:
        """
        Check if an open position should be closed at the current price.

        Returns:
            (should_exit, reason, exit_price)
        """
        # Time-based exit
        if pos.age_hours >= self._risk.max_hold_hours:
            return True, "TIME_EXIT", current_price

        if pos.side == PositionSide.LONG:
            # Trailing stop hit
            if pos.trailing_stop_active and current_price <= pos.trailing_stop_price:
                return True, "TRAILING", current_price
            # Hard SL
            if current_price <= pos.stop_loss:
                return True, "SL", pos.stop_loss
            # TP
            if current_price >= pos.take_profit:
                return True, "TP", pos.take_profit
        else:
            if pos.trailing_stop_active and current_price >= pos.trailing_stop_price:
                return True, "TRAILING", current_price
            if current_price >= pos.stop_loss:
                return True, "SL", pos.stop_loss
            if current_price <= pos.take_profit:
                return True, "TP", pos.take_profit

        return False, "", 0.0

    def evaluate_partial_tp(
        self, pos: Position, current_price: float
    ) -> bool:
        """Return True if partial TP level is reached and not yet taken."""
        if pos.partial_tp_done:
            return False
        if pos.side == PositionSide.LONG:
            return current_price >= pos.partial_tp_price
        return current_price <= pos.partial_tp_price

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _round_quantity(qty: float, symbol_info: dict) -> float:
        """Round quantity to exchange lot-size step."""
        step = 0.001  # default fallback
        for f in symbol_info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                step = float(f.get("stepSize", step))
                break
        precision = max(0, -int(math.floor(math.log10(step))))
        return math.floor(qty * 10 ** precision) / 10 ** precision

    @staticmethod
    def _get_min_notional(symbol_info: dict) -> float:
        """Return minimum notional value from exchange filters."""
        for f in symbol_info.get("filters", []):
            if f.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
                return float(f.get("minNotional", f.get("notional", 10.0)))
        return 10.0
