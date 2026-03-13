"""
execution/order_manager.py
==========================
Order execution layer — LIMIT-first with MARKET fallback.

Design principles:
  • Default to LIMIT (maker) orders to minimise fees.
  • If not filled within `limit_order_timeout_s`, cancel and replace with MARKET.
  • Retry transient Binance API errors up to `max_retries` times.
  • Validate slippage before confirming fill.
  • Supports both Spot and USDT-Margined Futures.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from binance import AsyncClient
from binance.enums import (
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    SIDE_BUY,
    SIDE_SELL,
    TIME_IN_FORCE_GTC,
    FUTURE_ORDER_TYPE_LIMIT,
    FUTURE_ORDER_TYPE_MARKET,
)
from binance.exceptions import BinanceAPIException, BinanceOrderException
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from core.config import BotConfig
from core.logger import logger
from core.state import BotState, Position, PositionSide, TradeRecord
from risk.manager import RiskManager, TradeSetup


class OrderManager:
    """
    Handles the full order lifecycle for one trade:
      1. Place limit entry order
      2. Poll for fill or timeout → fallback to market
      3. Place SL/TP orders (OCO for spot, separate orders for futures)
      4. Poll for exit conditions
      5. Record closed trade
    """

    def __init__(
        self,
        client: AsyncClient,
        config: BotConfig,
        state: BotState,
        risk_manager: RiskManager,
    ) -> None:
        self._client = client
        self._cfg = config
        self._exec = config.execution
        self._state = state
        self._risk = risk_manager
        self._is_futures = config.exchange.mode == "futures"

    # ── Entry ─────────────────────────────────────────────────────────────────

    async def enter_position(self, setup: TradeSetup) -> Optional[Position]:
        """
        Place entry order and open position on fill.

        Args:
            setup: Pre-computed trade parameters from RiskManager.

        Returns:
            Position object if entry filled, else None.
        """
        side = SIDE_BUY if setup.side == PositionSide.LONG else SIDE_SELL
        symbol = setup.symbol

        # Round price to tick size
        entry_price = self._round_price(setup.entry_price, symbol)

        fill_price: Optional[float] = None
        order_id: Optional[str] = None

        if self._exec.use_limit_orders:
            fill_price, order_id = await self._place_limit_entry(
                symbol, side, setup.quantity, entry_price
            )
        if fill_price is None:
            # Fallback to market
            fill_price, order_id = await self._place_market_order(
                symbol, side, setup.quantity
            )

        if fill_price is None:
            logger.error(f"Entry order failed for {symbol} after all retries")
            return None

        # Slippage check
        if not self._risk.check_slippage(setup.entry_price, fill_price):
            logger.warning(f"Slippage guard rejected fill for {symbol} — closing position")
            await self._close_at_market(symbol, side, setup.quantity)
            return None

        # Build Position
        pos = Position(
            symbol=symbol,
            side=setup.side,
            entry_price=fill_price,
            quantity=setup.quantity,
            stop_loss=setup.stop_loss,
            take_profit=setup.take_profit,
            atr_at_entry=setup.atr,
            partial_tp_price=setup.partial_tp_price,
            risk_amount=setup.risk_usdt,
            entry_order_id=order_id,
        )
        await self._state.open_position(pos)
        logger.info(
            f"Opened {setup.side.value} {symbol}: entry={fill_price:.4f} "
            f"SL={setup.stop_loss:.4f} TP={setup.take_profit:.4f} "
            f"qty={setup.quantity}"
        )
        return pos

    # ── Exit ──────────────────────────────────────────────────────────────────

    async def exit_position(
        self,
        pos: Position,
        reason: str,
        exit_price: Optional[float] = None,
        quantity: Optional[float] = None,
    ) -> Optional[TradeRecord]:
        """
        Close an open position (full or partial).

        Args:
            pos:        The position to close.
            reason:     Exit reason label.
            exit_price: Limit price; None = market order.
            quantity:   Quantity to close; None = full position.

        Returns:
            TradeRecord on success, None on failure.
        """
        qty = quantity or pos.quantity
        close_side = SIDE_SELL if pos.side == PositionSide.LONG else SIDE_BUY

        if exit_price and self._exec.use_limit_orders:
            fill, oid = await self._place_limit_exit(pos.symbol, close_side, qty, exit_price)
        else:
            fill, oid = await self._place_market_order(pos.symbol, close_side, qty)

        if fill is None:
            logger.error(f"Exit order failed for {pos.symbol}")
            return None

        # PnL calculation
        fees = fill * qty * 0.001  # conservative 0.1% taker fee
        if pos.side == PositionSide.LONG:
            pnl = (fill - pos.entry_price) * qty - fees
        else:
            pnl = (pos.entry_price - fill) * qty - fees
        pnl_pct = pnl / (pos.entry_price * pos.quantity)

        trade = TradeRecord(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=fill,
            quantity=qty,
            pnl_usdt=round(pnl, 4),
            pnl_pct=round(pnl_pct, 6),
            opened_at=pos.opened_at,
            closed_at=datetime.now(timezone.utc),
            exit_reason=reason,
            fees_usdt=round(fees, 4),
        )

        # Remove from state only if full close
        if quantity is None or abs(quantity - pos.quantity) < 1e-8:
            await self._state.close_position(pos.symbol)
        else:
            await self._state.update_position(
                pos.symbol, quantity=pos.quantity - qty, partial_tp_done=True
            )

        await self._state.record_trade(trade)
        logger.info(
            f"Closed {pos.side.value} {pos.symbol} [{reason}]: "
            f"fill={fill:.4f} PnL={pnl:.2f} USDT ({pnl_pct:.2%})"
        )
        return trade

    # ── Order placers ─────────────────────────────────────────────────────────

    async def _place_limit_entry(
        self, symbol: str, side: str, qty: float, price: float
    ) -> Tuple[Optional[float], Optional[str]]:
        """Place LIMIT order and poll for fill with timeout fallback."""
        try:
            order = await self._with_retry(
                self._place_order_raw,
                symbol=symbol,
                side=side,
                order_type=ORDER_TYPE_LIMIT,
                quantity=qty,
                price=price,
                time_in_force=TIME_IN_FORCE_GTC,
            )
        except Exception as exc:
            logger.error(f"Limit order placement error {symbol}: {exc}")
            return None, None

        order_id = str(order["orderId"])
        return await self._poll_limit_order(symbol, order_id, side, qty)

    async def _poll_limit_order(
        self, symbol: str, order_id: str, side: str, qty: float
    ) -> Tuple[Optional[float], Optional[str]]:
        """Poll LIMIT order until filled, timeout, or cancelled."""
        timeout = self._exec.limit_order_timeout_s
        poll_interval = 0.5
        elapsed = 0.0

        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            try:
                status = await self._get_order(symbol, order_id)
                if status["status"] == "FILLED":
                    fill = float(status.get("avgPrice") or status.get("price", 0))
                    return fill, order_id
                if status["status"] in ("CANCELED", "REJECTED", "EXPIRED"):
                    return None, None
            except Exception as exc:
                logger.warning(f"Order poll error {symbol}/{order_id}: {exc}")

        # Timeout — cancel and return None so caller falls back to MARKET
        try:
            await self._cancel_order(symbol, order_id)
        except Exception:
            pass
        logger.info(f"Limit order {order_id} timed out — falling back to MARKET")
        return None, None

    async def _place_limit_exit(
        self, symbol: str, side: str, qty: float, price: float
    ) -> Tuple[Optional[float], Optional[str]]:
        """Place limit exit with short poll, fallback to market."""
        try:
            order = await self._with_retry(
                self._place_order_raw,
                symbol=symbol,
                side=side,
                order_type=ORDER_TYPE_LIMIT,
                quantity=qty,
                price=price,
                time_in_force=TIME_IN_FORCE_GTC,
            )
            order_id = str(order["orderId"])
            fill, oid = await self._poll_limit_order(symbol, order_id, side, qty)
            if fill:
                return fill, oid
        except Exception as exc:
            logger.warning(f"Limit exit error {symbol}: {exc}")

        return await self._place_market_order(symbol, side, qty)

    async def _place_market_order(
        self, symbol: str, side: str, qty: float
    ) -> Tuple[Optional[float], Optional[str]]:
        """Place MARKET order. Returns (fill_price, order_id)."""
        try:
            order = await self._with_retry(
                self._place_order_raw,
                symbol=symbol,
                side=side,
                order_type=ORDER_TYPE_MARKET,
                quantity=qty,
            )
            fill = float(order.get("fills", [{}])[0].get("price", 0) or
                         order.get("avgPrice", 0) or order.get("price", 0))
            return fill, str(order["orderId"])
        except Exception as exc:
            logger.error(f"Market order error {symbol}: {exc}")
            return None, None

    async def _close_at_market(self, symbol: str, entry_side: str, qty: float) -> None:
        """Emergency market close (for slippage rejection)."""
        close_side = SIDE_SELL if entry_side == SIDE_BUY else SIDE_BUY
        await self._place_market_order(symbol, close_side, qty)

    # ── Exchange API wrappers ─────────────────────────────────────────────────

    async def _place_order_raw(self, **kwargs) -> Dict[str, Any]:
        """Route to spot or futures order endpoint."""
        if self._is_futures:
            return await self._client.futures_create_order(**kwargs)
        return await self._client.create_order(**kwargs)

    async def _get_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        if self._is_futures:
            return await self._client.futures_get_order(symbol=symbol, orderId=order_id)
        return await self._client.get_order(symbol=symbol, orderId=order_id)

    async def _cancel_order(self, symbol: str, order_id: str) -> None:
        if self._is_futures:
            await self._client.futures_cancel_order(symbol=symbol, orderId=order_id)
        else:
            await self._client.cancel_order(symbol=symbol, orderId=order_id)

    # ── Retry wrapper ─────────────────────────────────────────────────────────

    async def _with_retry(self, fn, **kwargs) -> Any:
        """Retry transient API errors with fixed delay."""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._exec.max_retries),
            wait=wait_fixed(self._exec.retry_delay_s),
            retry=retry_if_exception_type((BinanceAPIException, asyncio.TimeoutError)),
            reraise=True,
        ):
            with attempt:
                return await fn(**kwargs)

    # ── Price rounding ────────────────────────────────────────────────────────

    def _round_price(self, price: float, symbol: str) -> float:
        """Round price to 2 decimal places (adequate for most pairs; improve with tick-size)."""
        return round(price, 2)
