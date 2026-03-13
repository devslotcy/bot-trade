"""
main.py
=======
Async bot entrypoint.

Architecture:
  • Main asyncio loop coordinates all components.
  • KlineStreamManager → WebSocket data → DataBuffer → Strategy → RiskManager → OrderManager.
  • Heartbeat task (60 s) checks bot health + daily reset.
  • Daily summary task sends Telegram PnL digest.
  • Graceful shutdown on SIGTERM / KeyboardInterrupt.
  • Circuit-breaker: >5 consecutive failures → 20-minute pause.

Run:
    python main.py
    python main.py --config config.yaml --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
from binance import AsyncClient

from core.config import load_config, get_secrets, BotConfig
from core.logger import logger, setup_logger
from core.state import BotState, BotStatus, PositionSide
from data.fetcher import DataFetcher, KlineStreamManager
from data.indicators import compute_all_indicators, merge_higher_tf
from execution.order_manager import OrderManager
from risk.manager import RiskManager
from strategies.trend_momentum import TrendMomentumStrategy, SignalType
from utils.database import Database
from utils.telegram import TelegramNotifier


# ---------------------------------------------------------------------------
# Data buffer — rolling window of OHLCV per symbol/interval
# ---------------------------------------------------------------------------

class DataBuffer:
    """
    Maintains a rolling OHLCV DataFrame per symbol/interval.
    Updated on every closed kline from WebSocket.
    """

    def __init__(self, max_bars: int = 600) -> None:
        self._max = max_bars
        self._data: Dict[str, Dict[str, pd.DataFrame]] = defaultdict(dict)

    def update(self, symbol: str, interval: str, bar: pd.Series) -> None:
        key = f"{symbol}_{interval}"
        sym_store = self._data[symbol]
        df = sym_store.get(interval, pd.DataFrame())
        new_row = pd.DataFrame([bar])
        df = pd.concat([df, new_row])
        df = df[~df.index.duplicated(keep="last")]
        df.sort_index(inplace=True)
        if len(df) > self._max:
            df = df.iloc[-self._max:]
        sym_store[interval] = df

    def get(self, symbol: str, interval: str) -> Optional[pd.DataFrame]:
        return self._data.get(symbol, {}).get(interval)

    def seed(self, symbol: str, interval: str, df: pd.DataFrame) -> None:
        """Pre-load historical data before WS starts."""
        self._data[symbol][interval] = df.copy()


# ---------------------------------------------------------------------------
# Main bot class
# ---------------------------------------------------------------------------

class TradingBot:
    """
    Orchestrates all components for live trading.

    Lifecycle:
        bot = TradingBot(config, dry_run=False)
        await bot.start()  ← runs forever
        await bot.stop()   ← graceful shutdown
    """

    def __init__(self, config: BotConfig, dry_run: bool = False) -> None:
        self._cfg = config
        self._dry_run = dry_run
        self._state = BotState()
        self._buffer = DataBuffer()
        self._client: Optional[AsyncClient] = None
        self._fetcher: Optional[DataFetcher] = None
        self._stream_mgr: Optional[KlineStreamManager] = None
        self._order_mgr: Optional[OrderManager] = None
        self._risk_mgr: Optional[RiskManager] = None
        self._strategy: Optional[TrendMomentumStrategy] = None
        self._notifier: Optional[TelegramNotifier] = None
        self._db: Optional[Database] = None
        self._shutdown_event = asyncio.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialise all components and begin the main loop."""
        logger.info("=== Trading Bot Starting ===")

        secrets = get_secrets()

        # Binance client
        self._client = await AsyncClient.create(
            api_key=secrets.binance_api_key,
            api_secret=secrets.binance_api_secret,
            testnet=self._cfg.exchange.testnet,
        )

        # Components
        self._fetcher = DataFetcher(self._client, self._cfg)
        self._state.equity = await self._fetcher.get_account_balance("USDT")
        await self._state.update_equity(self._state.equity)
        await self._state.reset_daily(self._state.equity)

        self._strategy = TrendMomentumStrategy(self._cfg)
        self._risk_mgr = RiskManager(self._cfg, self._state)
        self._order_mgr = OrderManager(self._client, self._cfg, self._state, self._risk_mgr)

        # Database
        self._db = Database(self._cfg.database.path)
        await self._db.init()

        # Telegram
        if (self._cfg.notifications.telegram_enabled and
                secrets.telegram_bot_token and secrets.telegram_chat_id):
            self._notifier = TelegramNotifier(
                self._cfg, self._state,
                secrets.telegram_bot_token, secrets.telegram_chat_id
            )
            await self._notifier.notify_status("STARTED")

        # Seed historical data for all symbols + both timeframes
        await self._seed_historical_data()

        # WebSocket streams
        self._stream_mgr = KlineStreamManager(
            self._client, self._cfg, self._on_kline
        )
        intervals = [self._cfg.timeframes.entry, self._cfg.timeframes.trend]
        await self._stream_mgr.start(self._cfg.symbols, intervals)

        await self._state.set_status(BotStatus.RUNNING)
        logger.info(
            f"Bot running. Equity: {self._state.equity:.2f} USDT | "
            f"Symbols: {self._cfg.symbols} | Mode: {self._cfg.exchange.mode}"
        )

        # Launch background tasks
        tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._daily_summary_loop(), name="daily_summary"),
            asyncio.create_task(self._position_monitor_loop(), name="position_monitor"),
            asyncio.create_task(self._equity_snapshot_loop(), name="equity_snapshot"),
        ]

        # Wait for shutdown signal
        await self._shutdown_event.wait()

        # Cleanup
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.stop()

    async def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down bot…")
        await self._state.set_status(BotStatus.STOPPED)
        if self._stream_mgr:
            await self._stream_mgr.stop()
        if self._notifier:
            await self._notifier.notify_status("STOPPED")
        if self._db:
            await self._db.close()
        if self._client:
            await self._client.close_connection()
        logger.info("Bot stopped cleanly.")

    def request_shutdown(self) -> None:
        """Signal the bot to shut down (called by signal handlers)."""
        self._shutdown_event.set()

    # ── Data seeding ──────────────────────────────────────────────────────────

    async def _seed_historical_data(self) -> None:
        """Download and buffer the last N bars for all symbols × timeframes."""
        s = self._cfg.strategy
        r = self._cfg.risk
        for symbol in self._cfg.symbols:
            for interval in [self._cfg.timeframes.entry, self._cfg.timeframes.trend]:
                try:
                    df = await self._fetcher.get_klines(symbol, interval, limit=500)
                    df_ind = compute_all_indicators(
                        df,
                        ema_fast=s.ema_fast, ema_slow=s.ema_slow,
                        supertrend_period=s.supertrend_period,
                        supertrend_multiplier=s.supertrend_multiplier,
                        rsi_period=s.rsi_period,
                        macd_fast=s.macd_fast, macd_slow=s.macd_slow,
                        macd_signal=s.macd_signal,
                        atr_period=r.atr_period,
                        volume_sma_period=s.volume_sma_period,
                        pullback_ema=s.pullback_ema,
                        use_supertrend=s.use_supertrend,
                    )
                    self._buffer.seed(symbol, interval, df_ind)
                    logger.info(f"Seeded {symbol}/{interval}: {len(df_ind)} bars")
                except Exception as exc:
                    logger.error(f"Seed error {symbol}/{interval}: {exc}")

    # ── WebSocket callback ────────────────────────────────────────────────────

    def _on_kline(self, symbol: str, interval: str, bar: pd.Series) -> None:
        """Called for every closed kline bar from WS."""
        # Update buffer (append new bar)
        existing = self._buffer.get(symbol, interval)
        if existing is not None:
            # Recompute indicators for last N bars only
            s = self._cfg.strategy
            r = self._cfg.risk
            tail = existing.iloc[-300:].copy()
            # Append raw bar to the underlying OHLCV before indicator recompute
            # (For speed, we just append and recompute indicators)
            new_row = pd.DataFrame(
                {col: [bar.get(col, 0.0)] for col in ["open", "high", "low", "close", "volume", "trades"]},
                index=[bar.name]
            )
            combined = pd.concat([tail, new_row])
            combined = combined[~combined.index.duplicated(keep="last")]
            try:
                df_ind = compute_all_indicators(
                    combined,
                    ema_fast=s.ema_fast, ema_slow=s.ema_slow,
                    supertrend_period=s.supertrend_period,
                    supertrend_multiplier=s.supertrend_multiplier,
                    rsi_period=s.rsi_period,
                    macd_fast=s.macd_fast, macd_slow=s.macd_slow,
                    macd_signal=s.macd_signal,
                    atr_period=r.atr_period,
                    volume_sma_period=s.volume_sma_period,
                    pullback_ema=s.pullback_ema,
                    use_supertrend=s.use_supertrend,
                )
                self._buffer.seed(symbol, interval, df_ind)
            except Exception as exc:
                logger.warning(f"Indicator recompute error {symbol}/{interval}: {exc}")

        # Only trigger signal evaluation on entry-timeframe bars
        if interval == self._cfg.timeframes.entry:
            asyncio.create_task(self._evaluate_signal(symbol))

    # ── Signal evaluation ─────────────────────────────────────────────────────

    async def _evaluate_signal(self, symbol: str) -> None:
        """Evaluate strategy signal and execute if valid."""
        if not self._state.is_running:
            return
        if self._state.circuit_breaker_active():
            return

        try:
            entry_df = self._buffer.get(symbol, self._cfg.timeframes.entry)
            trend_df = self._buffer.get(symbol, self._cfg.timeframes.trend)
            if entry_df is None or trend_df is None or len(entry_df) < 50:
                return

            # Merge higher-TF trend
            merged = merge_higher_tf(entry_df, trend_df, self._cfg.strategy.use_supertrend)

            # Get funding rate (futures only)
            funding = 0.0
            if self._cfg.exchange.mode == "futures":
                funding = await self._fetcher.get_funding_rate(symbol)

            # Strategy evaluation
            signal = self._strategy.evaluate(symbol, merged, funding)
            if signal.signal == SignalType.NONE:
                return

            logger.info(f"Signal: {signal.signal.value} {symbol} — {signal.reason}")

            # Pre-trade risk checks
            try:
                self._risk_mgr.check_pre_trade(symbol)
            except ValueError as e:
                logger.debug(f"Pre-trade check blocked {symbol}: {e}")
                return

            # Symbol info for lot-sizing
            sym_info = await self._fetcher.get_symbol_info(symbol)
            side = PositionSide.LONG if signal.signal == SignalType.LONG else PositionSide.SHORT

            # Compute trade parameters
            try:
                setup = self._risk_mgr.compute_setup(
                    symbol=symbol,
                    side=side,
                    entry_price=signal.entry_price,
                    atr=signal.atr,
                    symbol_info=sym_info,
                )
            except ValueError as e:
                logger.warning(f"Setup rejected {symbol}: {e}")
                return

            if self._dry_run:
                logger.info(f"[DRY RUN] Would enter {side.value} {symbol}: {setup}")
                return

            # Execute entry
            pos = await self._order_mgr.enter_position(setup)
            if pos and self._notifier:
                await self._notifier.notify_entry(
                    symbol=symbol, side=pos.side, price=pos.entry_price,
                    sl=pos.stop_loss, tp=pos.take_profit,
                    qty=pos.quantity, risk_usdt=pos.risk_amount,
                )

            # Reset circuit-breaker failures on success
            await self._state.reset_failures()

        except Exception as exc:
            logger.error(f"Signal evaluation error for {symbol}: {exc}")
            failures = await self._state.increment_failures(
                self._cfg.reliability.circuit_breaker_pause_min
            )
            if failures >= self._cfg.reliability.circuit_breaker_failures and self._notifier:
                await self._notifier.notify_circuit_breaker(
                    self._cfg.reliability.circuit_breaker_pause_min
                )

    # ── Position monitor ──────────────────────────────────────────────────────

    async def _position_monitor_loop(self) -> None:
        """Check open positions for SL/TP/trailing/time exits every 30 seconds."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(30)
            for symbol, pos in list(self._state.positions.items()):
                try:
                    entry_df = self._buffer.get(symbol, self._cfg.timeframes.entry)
                    if entry_df is None or entry_df.empty:
                        continue
                    current_price = float(entry_df.iloc[-1]["close"])

                    # Update trailing stop
                    atr_col = f"atr_{self._cfg.risk.atr_period}"
                    atr = float(entry_df.iloc[-1].get(atr_col, pos.atr_at_entry))
                    trail_active, trail_price = self._strategy.check_trailing_stop(
                        current_price=current_price,
                        entry_price=pos.entry_price,
                        side=pos.side,
                        atr=atr,
                        trailing_active=pos.trailing_stop_active,
                        trailing_price=pos.trailing_stop_price,
                        sl_atr_mult=self._cfg.risk.sl_atr_multiplier,
                        trail_atr_mult=self._cfg.risk.trailing_stop_atr_multiplier,
                        activation_r=self._cfg.risk.trailing_stop_activation_r,
                        sl_price=pos.stop_loss,
                    )
                    if trail_active != pos.trailing_stop_active or trail_price != pos.trailing_stop_price:
                        await self._state.update_position(
                            symbol,
                            trailing_stop_active=trail_active,
                            trailing_stop_price=trail_price,
                        )
                        pos.trailing_stop_active = trail_active
                        pos.trailing_stop_price = trail_price

                    # Partial TP
                    if self._risk_mgr.evaluate_partial_tp(pos, current_price):
                        trade = await self._order_mgr.exit_position(
                            pos, "PARTIAL_TP", pos.partial_tp_price,
                            quantity=pos.quantity * self._cfg.risk.partial_tp_pct,
                        )
                        if trade and self._db:
                            await self._db.save_trade(trade)
                        if trade and self._notifier:
                            await self._notifier.notify_exit(trade)

                    # Full exit check
                    should_exit, reason, exit_price = self._risk_mgr.evaluate_exit(
                        pos, current_price
                    )
                    if should_exit:
                        trade = await self._order_mgr.exit_position(
                            pos, reason, exit_price if reason != "TIME_EXIT" else None
                        )
                        if trade and self._db:
                            await self._db.save_trade(trade)
                        if trade and self._notifier:
                            await self._notifier.notify_exit(trade)

                        # Check daily loss limit after exit
                        await self._check_daily_limit()

                except Exception as exc:
                    logger.error(f"Position monitor error for {symbol}: {exc}")

    async def _check_daily_limit(self) -> None:
        """Halt bot if daily loss limit exceeded."""
        if self._state.daily.loss_pct <= self._cfg.risk.daily_loss_limit:
            await self._state.set_status(BotStatus.DAILY_LIMIT_HIT)
            logger.warning(
                f"Daily loss limit hit: {self._state.daily.loss_pct:.2%}. "
                "Trading halted until next UTC day."
            )
            if self._notifier:
                await self._notifier.notify_daily_limit(self._state.daily.loss_pct)

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Periodic health check and daily reset."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(self._cfg.reliability.heartbeat_interval_s)
            try:
                # Refresh equity
                equity = await self._fetcher.get_account_balance("USDT")
                await self._state.update_equity(equity)

                # Daily reset at UTC midnight
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if self._state.daily.date and self._state.daily.date != today:
                    if self._notifier:
                        await self._notifier.notify_daily_summary()
                    await self._state.reset_daily(equity)
                    # Re-enable bot if it was halted by daily limit
                    if self._state.status == BotStatus.DAILY_LIMIT_HIT:
                        await self._state.set_status(BotStatus.RUNNING)
                        logger.info("New UTC day — daily limit reset, resuming trading")

                # Re-enable if circuit breaker expired
                if (self._state.status == BotStatus.CIRCUIT_BREAKER and
                        not self._state.circuit_breaker_active()):
                    await self._state.set_status(BotStatus.RUNNING)
                    logger.info("Circuit breaker cleared — resuming trading")

                logger.debug(
                    f"Heartbeat: equity={equity:.2f} status={self._state.status.value} "
                    f"positions={self._state.open_count}"
                )
            except Exception as exc:
                logger.error(f"Heartbeat error: {exc}")

    # ── Daily summary ─────────────────────────────────────────────────────────

    async def _daily_summary_loop(self) -> None:
        """Send Telegram daily summary at configured UTC hour."""
        target_hour = self._cfg.notifications.daily_summary_utc_hour
        sent_today = False
        while not self._shutdown_event.is_set():
            await asyncio.sleep(60)
            now = datetime.now(timezone.utc)
            if now.hour == target_hour and not sent_today:
                if self._notifier:
                    await self._notifier.notify_daily_summary()
                sent_today = True
            elif now.hour != target_hour:
                sent_today = False

    # ── Equity snapshots ──────────────────────────────────────────────────────

    async def _equity_snapshot_loop(self) -> None:
        """Save equity to DB every 5 minutes."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(300)
            try:
                if self._db:
                    await self._db.save_equity(
                        datetime.now(timezone.utc), self._state.equity
                    )
            except Exception as exc:
                logger.warning(f"Equity snapshot error: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    setup_logger(cfg.logging.level, cfg.logging.log_dir,
                 cfg.logging.rotate_size_mb, cfg.logging.retention_days)

    bot = TradingBot(cfg, dry_run=args.dry_run)

    # Handle OS signals for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, bot.request_shutdown)

    try:
        await bot.start()
    except Exception as exc:
        logger.critical(f"Fatal bot error: {exc}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crypto Trading Bot")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="Evaluate signals but skip order execution")
    args = parser.parse_args()
    asyncio.run(main(args))
