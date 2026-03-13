"""
utils/telegram.py
=================
Async Telegram notification service.

Sends alerts for:
  • Trade entries and exits (with PnL)
  • Daily PnL summary
  • Critical errors / circuit breaker events
  • Bot start / stop

All sends are fire-and-forget with a single retry so bot loop is never blocked.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError

from core.config import BotConfig
from core.logger import logger
from core.state import BotState, TradeRecord, PositionSide


class TelegramNotifier:
    """
    Wraps python-telegram-bot's async Bot for notifications.

    Usage:
        notifier = TelegramNotifier(config, state, bot_token, chat_id)
        await notifier.send("Hello from bot!")
    """

    def __init__(
        self,
        config: BotConfig,
        state: BotState,
        bot_token: str,
        chat_id: str,
    ) -> None:
        self._cfg = config
        self._state = state
        self._bot = Bot(token=bot_token)
        self._chat_id = chat_id
        self._enabled = config.notifications.telegram_enabled

    # ── Public API ────────────────────────────────────────────────────────────

    async def send(self, message: str, parse_mode: str = "Markdown") -> None:
        """Send a plain message. Non-blocking (fire-and-forget with retry)."""
        if not self._enabled:
            return
        asyncio.create_task(self._send_with_retry(message, parse_mode))

    async def notify_entry(self, symbol: str, side: PositionSide, price: float,
                           sl: float, tp: float, qty: float, risk_usdt: float) -> None:
        """Send trade entry notification."""
        side_emoji = "" if side == PositionSide.LONG else ""
        msg = (
            f"{side_emoji} *NEW {side.value}* — `{symbol}`\n"
            f"  Entry  : `{price:.4f}`\n"
            f"  Stop   : `{sl:.4f}`\n"
            f"  Target : `{tp:.4f}`\n"
            f"  Qty    : `{qty}`\n"
            f"  Risk   : `{risk_usdt:.2f} USDT`\n"
            f"  _at {self._ts()}_"
        )
        await self.send(msg)

    async def notify_exit(self, trade: TradeRecord) -> None:
        """Send trade exit notification."""
        pnl_sign = "" if trade.pnl_usdt >= 0 else ""
        emoji = "" if trade.pnl_usdt >= 0 else ""
        msg = (
            f"{emoji} *CLOSED {trade.side.value}* — `{trade.symbol}`\n"
            f"  Exit   : `{trade.exit_price:.4f}` [{trade.exit_reason}]\n"
            f"  PnL    : {pnl_sign}`{trade.pnl_usdt:+.2f} USDT` ({trade.pnl_pct:+.2%})\n"
            f"  Fees   : `{trade.fees_usdt:.4f} USDT`\n"
            f"  Held   : `{(trade.closed_at - trade.opened_at)}` \n"
            f"  _at {self._ts()}_"
        )
        await self.send(msg)

    async def notify_daily_summary(self) -> None:
        """Send end-of-day PnL summary."""
        d = self._state.daily
        equity = self._state.equity
        pnl_sign = "" if d.realized_pnl >= 0 else ""
        emoji = "" if d.realized_pnl >= 0 else ""
        msg = (
            f"{emoji} *Daily Summary* — {d.date}\n"
            f"  Equity   : `{equity:.2f} USDT`\n"
            f"  Day PnL  : {pnl_sign}`{d.realized_pnl:+.2f} USDT` ({d.loss_pct:+.2%})\n"
            f"  Trades   : `{d.trade_count}`\n"
            f"  Win Rate : `{d.win_rate:.1%}`"
        )
        await self.send(msg)

    async def notify_error(self, error: str, critical: bool = False) -> None:
        """Send error alert."""
        emoji = "" if critical else ""
        prefix = "CRITICAL" if critical else "ERROR"
        msg = f"{emoji} *{prefix}*\n```\n{error[:1000]}\n```\n_at {self._ts()}_"
        await self.send(msg)

    async def notify_status(self, status: str) -> None:
        """Send bot status change notification."""
        msg = f" *Bot Status* → `{status}`\n_at {self._ts()}_"
        await self.send(msg)

    async def notify_circuit_breaker(self, pause_min: int) -> None:
        """Notify circuit breaker activation."""
        msg = (
            f" *Circuit Breaker*\n"
            f"  Bot paused for `{pause_min}` minutes due to repeated errors.\n"
            f"  _at {self._ts()}_"
        )
        await self.send(msg)

    async def notify_daily_limit(self, loss_pct: float) -> None:
        """Notify daily loss limit hit."""
        msg = (
            f" *Daily Loss Limit Hit*\n"
            f"  Loss: `{loss_pct:.2%}`\n"
            f"  Trading halted until next UTC day.\n"
            f"  _at {self._ts()}_"
        )
        await self.send(msg)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _send_with_retry(self, message: str, parse_mode: str) -> None:
        """Send with one retry on TelegramError."""
        for attempt in range(2):
            try:
                await self._bot.send_message(
                    chat_id=self._chat_id,
                    text=message,
                    parse_mode=parse_mode,
                )
                return
            except TelegramError as exc:
                if attempt == 0:
                    await asyncio.sleep(5)
                else:
                    logger.warning(f"Telegram send failed: {exc}")
            except Exception as exc:
                logger.warning(f"Telegram unexpected error: {exc}")
                return

    @staticmethod
    def _ts() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
