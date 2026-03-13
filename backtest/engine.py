"""
backtest/engine.py
==================
Vectorized backtesting engine with walk-forward validation.

Design:
  • Simulates bar-by-bar on pre-downloaded historical OHLCV + indicators.
  • Applies the same signal logic as the live strategy (no look-ahead).
  • Realistic fee model (configurable %).
  • Outputs full metrics + equity curve CSV.
  • Walk-forward: splits data into N folds, each with in/out-of-sample periods.

Usage:
    python -m backtest.engine --symbol BTCUSDT --interval 1h
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Ensure project root is on path when run as script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import BotConfig, load_config
from core.logger import logger, setup_logger
from data.fetcher import DataFetcher
from data.indicators import compute_all_indicators, merge_higher_tf
from strategies.trend_momentum import TrendMomentumStrategy, SignalType
from core.state import PositionSide


# ---------------------------------------------------------------------------
# Trade simulation record
# ---------------------------------------------------------------------------

@dataclass
class BacktestTrade:
    symbol: str
    side: PositionSide
    entry_bar: int
    exit_bar: int
    entry_price: float
    exit_price: float
    quantity: float
    pnl_usdt: float
    pnl_pct: float
    exit_reason: str
    fees_usdt: float


@dataclass
class BacktestResult:
    """Complete backtest output."""

    symbol: str
    period: str
    initial_capital: float
    final_equity: float
    total_return_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float
    sqn: float
    total_trades: int
    avg_trade_pnl: float
    equity_curve: pd.Series
    trades: List[BacktestTrade] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """
    Single-symbol vectorized (bar-by-bar) backtest engine.

    Args:
        config: Bot configuration.
        entry_df: Entry-timeframe OHLCV + all indicators + 'htf_trend'.
        initial_capital: Starting USDT.
        commission_pct: Per-side fee as fraction (0.001 = 0.1%).
    """

    def __init__(
        self,
        config: BotConfig,
        entry_df: pd.DataFrame,
        initial_capital: float = 10_000.0,
        commission_pct: float = 0.001,
        symbol: str = "BTCUSDT",
    ) -> None:
        self._cfg = config
        self._df = entry_df.copy()
        self._capital = initial_capital
        self._commission = commission_pct
        self._symbol = symbol
        self._strategy = TrendMomentumStrategy(config)
        self._risk = config.risk

    def run(self) -> BacktestResult:
        """Execute bar-by-bar simulation. Returns BacktestResult."""
        df = self._df
        n = len(df)

        equity = self._capital
        equity_series = np.zeros(n)
        trades: List[BacktestTrade] = []

        # State
        in_trade = False
        pos_side: Optional[PositionSide] = None
        entry_price = 0.0
        stop_loss = 0.0
        take_profit = 0.0
        partial_tp = 0.0
        partial_done = False
        trail_active = False
        trail_price = 0.0
        qty = 0.0
        risk_usdt = 0.0
        atr_entry = 0.0
        r_dist = 0.0
        entry_bar = 0

        for i in range(len(df)):
            bar = df.iloc[i]
            close = float(bar["close"])
            high = float(bar["high"])
            low = float(bar["low"])
            atr_col = f"atr_{self._cfg.risk.atr_period}"
            atr = float(bar.get(atr_col, bar.get("atr_14", 0.0)))

            equity_series[i] = equity

            if in_trade:
                # Update trailing stop
                trail_active, trail_price = self._strategy.check_trailing_stop(
                    current_price=close,
                    entry_price=entry_price,
                    side=pos_side,
                    atr=atr_entry,
                    trailing_active=trail_active,
                    trailing_price=trail_price,
                    sl_atr_mult=self._risk.sl_atr_multiplier,
                    trail_atr_mult=self._risk.trailing_stop_atr_multiplier,
                    activation_r=self._risk.trailing_stop_activation_r,
                    sl_price=stop_loss,
                )

                # Check exit conditions (use bar high/low for realistic fill)
                exit_price, exit_reason = self._check_exits(
                    pos_side, high, low, close,
                    stop_loss, take_profit, trail_active, trail_price,
                    entry_bar, i
                )

                # Partial TP
                if not partial_done and exit_reason is None:
                    if pos_side == PositionSide.LONG and high >= partial_tp:
                        partial_exit = partial_tp
                        partial_qty = qty * self._risk.partial_tp_pct
                        fee = partial_exit * partial_qty * self._commission
                        pnl = (partial_exit - entry_price) * partial_qty - fee * 2
                        equity += pnl
                        qty -= partial_qty
                        partial_done = True
                        trades.append(BacktestTrade(
                            symbol=self._symbol,
                            side=pos_side,
                            entry_bar=entry_bar,
                            exit_bar=i,
                            entry_price=entry_price,
                            exit_price=partial_exit,
                            quantity=partial_qty,
                            pnl_usdt=round(pnl, 4),
                            pnl_pct=round(pnl / (entry_price * partial_qty), 6),
                            exit_reason="PARTIAL_TP",
                            fees_usdt=round(fee * 2, 4),
                        ))
                    elif pos_side == PositionSide.SHORT and low <= partial_tp:
                        partial_exit = partial_tp
                        partial_qty = qty * self._risk.partial_tp_pct
                        fee = partial_exit * partial_qty * self._commission
                        pnl = (entry_price - partial_exit) * partial_qty - fee * 2
                        equity += pnl
                        qty -= partial_qty
                        partial_done = True
                        trades.append(BacktestTrade(
                            symbol=self._symbol, side=pos_side, entry_bar=entry_bar,
                            exit_bar=i, entry_price=entry_price, exit_price=partial_exit,
                            quantity=partial_qty, pnl_usdt=round(pnl, 4),
                            pnl_pct=round(pnl / (entry_price * partial_qty), 6),
                            exit_reason="PARTIAL_TP", fees_usdt=round(fee * 2, 4),
                        ))

                if exit_reason:
                    fee = exit_price * qty * self._commission
                    if pos_side == PositionSide.LONG:
                        pnl = (exit_price - entry_price) * qty - fee * 2
                    else:
                        pnl = (entry_price - exit_price) * qty - fee * 2
                    equity += pnl
                    trades.append(BacktestTrade(
                        symbol=self._symbol, side=pos_side, entry_bar=entry_bar,
                        exit_bar=i, entry_price=entry_price, exit_price=exit_price,
                        quantity=qty, pnl_usdt=round(pnl, 4),
                        pnl_pct=round(pnl / (entry_price * qty), 6),
                        exit_reason=exit_reason, fees_usdt=round(fee * 2, 4),
                    ))
                    in_trade = False
                    partial_done = False
                    trail_active = False
                    trail_price = 0.0

            if not in_trade and i >= 5:
                # Evaluate signal on last 3 bars (pass slice)
                sub = df.iloc[max(0, i - 50): i + 1]
                signal_ev = self._strategy.evaluate(self._symbol, sub)

                if signal_ev.signal != SignalType.NONE:
                    sl_dist = self._risk.sl_atr_multiplier * signal_ev.atr
                    risk_usdt = equity * self._risk.risk_per_trade
                    qty_raw = risk_usdt / sl_dist if sl_dist > 0 else 0.0
                    qty = math.floor(qty_raw * 1000) / 1000  # simple lot rounding

                    if qty <= 0:
                        continue

                    entry_price = close
                    atr_entry = signal_ev.atr
                    r_dist = sl_dist

                    if signal_ev.signal == SignalType.LONG:
                        pos_side = PositionSide.LONG
                        stop_loss = entry_price - sl_dist
                        take_profit = entry_price + self._risk.tp_r_ratio * sl_dist
                        partial_tp = entry_price + self._risk.partial_tp_r * sl_dist
                    else:
                        pos_side = PositionSide.SHORT
                        stop_loss = entry_price + sl_dist
                        take_profit = entry_price - self._risk.tp_r_ratio * sl_dist
                        partial_tp = entry_price - self._risk.partial_tp_r * sl_dist

                    # Fee for entry
                    fee = entry_price * qty * self._commission
                    equity -= fee
                    in_trade = True
                    entry_bar = i
                    trail_active = False
                    trail_price = 0.0
                    partial_done = False

        equity_series = pd.Series(equity_series, index=df.index)
        return self._compute_metrics(equity_series, trades, equity)

    def _check_exits(
        self, side, high, low, close,
        sl, tp, trail_active, trail_price, entry_bar, current_bar
    ) -> Tuple[float, Optional[str]]:
        """Return (fill_price, reason) or (0, None)."""
        bars_held = current_bar - entry_bar

        # Time exit
        if bars_held >= int(self._cfg.risk.max_hold_hours):  # 1h bars: hours = bars
            return close, "TIME_EXIT"

        if side == PositionSide.LONG:
            if trail_active and low <= trail_price:
                return trail_price, "TRAILING"
            if low <= sl:
                return sl, "SL"
            if high >= tp:
                return tp, "TP"
        else:
            if trail_active and high >= trail_price:
                return trail_price, "TRAILING"
            if high >= sl:
                return sl, "SL"
            if low <= tp:
                return tp, "TP"

        return 0.0, None

    def _compute_metrics(
        self,
        equity_curve: pd.Series,
        trades: List[BacktestTrade],
        final_equity: float,
    ) -> BacktestResult:
        """Calculate all performance metrics."""
        initial = self._capital

        # Returns series (bar-to-bar)
        returns = equity_curve.pct_change().dropna()

        # Drawdown
        roll_max = equity_curve.cummax()
        drawdown = (equity_curve - roll_max) / roll_max
        max_dd = float(drawdown.min())

        # Sharpe (annualised, 1h bars → 8760 per year)
        bars_per_year = 8760
        mean_ret = returns.mean()
        std_ret = returns.std()
        sharpe = (mean_ret / std_ret * math.sqrt(bars_per_year)) if std_ret > 0 else 0.0

        # Sortino (downside deviation only)
        neg_returns = returns[returns < 0]
        downside_std = neg_returns.std()
        sortino = (mean_ret / downside_std * math.sqrt(bars_per_year)) if downside_std > 0 else 0.0

        # Trade metrics
        pnls = [t.pnl_usdt for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rate = len(wins) / len(pnls) if pnls else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 1.0
        profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")

        # SQN = sqrt(n) * mean(R) / std(R)
        if len(pnls) > 1:
            r_values = np.array(pnls)
            sqn = math.sqrt(len(r_values)) * r_values.mean() / (r_values.std() + 1e-9)
        else:
            sqn = 0.0

        total_return = (final_equity - initial) / initial

        start = str(equity_curve.index[0].date()) if not equity_curve.empty else ""
        end = str(equity_curve.index[-1].date()) if not equity_curve.empty else ""

        return BacktestResult(
            symbol=self._symbol,
            period=f"{start} → {end}",
            initial_capital=initial,
            final_equity=round(final_equity, 2),
            total_return_pct=round(total_return * 100, 2),
            sharpe_ratio=round(sharpe, 3),
            sortino_ratio=round(sortino, 3),
            max_drawdown_pct=round(max_dd * 100, 2),
            win_rate=round(win_rate, 4),
            profit_factor=round(profit_factor, 3),
            sqn=round(sqn, 3),
            total_trades=len(trades),
            avg_trade_pnl=round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
            equity_curve=equity_curve,
            trades=trades,
        )


# ---------------------------------------------------------------------------
# Walk-forward validation
# ---------------------------------------------------------------------------

def walk_forward(
    config: BotConfig,
    entry_df: pd.DataFrame,
    trend_df: pd.DataFrame,
    symbol: str,
    n_splits: int = 5,
    in_sample_pct: float = 0.70,
) -> List[Dict]:
    """
    Walk-forward validation.

    Splits data into N sequential folds.
    For each fold: train on in-sample (ignored — strategy is parameter-free),
    test on out-of-sample. Returns list of per-fold metrics.
    """
    from data.indicators import merge_higher_tf

    results = []
    n = len(entry_df)
    fold_size = n // n_splits

    for i in range(n_splits):
        start = i * fold_size
        end = start + fold_size
        fold_df = entry_df.iloc[start:end].copy()

        # Forward-fill HTF trend onto fold
        fold_merged = merge_higher_tf(fold_df, trend_df, config.strategy.use_supertrend)

        # Split in/out
        split_idx = int(len(fold_merged) * in_sample_pct)
        oos_df = fold_merged.iloc[split_idx:].copy()

        if len(oos_df) < 50:
            logger.warning(f"WF fold {i+1}: out-of-sample too small ({len(oos_df)} bars)")
            continue

        engine = BacktestEngine(
            config=config,
            entry_df=oos_df,
            initial_capital=config.backtest.initial_capital,
            commission_pct=config.backtest.commission_pct,
            symbol=symbol,
        )
        result = engine.run()
        fold_result = {
            "fold": i + 1,
            "period": result.period,
            "return_pct": result.total_return_pct,
            "sharpe": result.sharpe_ratio,
            "max_dd_pct": result.max_drawdown_pct,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "total_trades": result.total_trades,
            "sqn": result.sqn,
        }
        results.append(fold_result)
        logger.info(
            f"WF Fold {i+1}/{n_splits} [{result.period}]: "
            f"Return={result.total_return_pct:.1f}% Sharpe={result.sharpe_ratio:.2f} "
            f"MaxDD={result.max_drawdown_pct:.1f}% WinRate={result.win_rate:.1%} "
            f"PF={result.profit_factor:.2f} Trades={result.total_trades}"
        )

    return results


def print_results(result: BacktestResult) -> None:
    """Pretty-print backtest metrics to console."""
    separator = "─" * 52
    print(f"\n{'═' * 52}")
    print(f"  BACKTEST RESULTS: {result.symbol}  [{result.period}]")
    print(f"{'═' * 52}")
    print(f"  Initial Capital  : ${result.initial_capital:,.2f}")
    print(f"  Final Equity     : ${result.final_equity:,.2f}")
    print(f"  Total Return     : {result.total_return_pct:+.2f}%")
    print(separator)
    print(f"  Sharpe Ratio     : {result.sharpe_ratio:.3f}  (target > 1.2)")
    print(f"  Sortino Ratio    : {result.sortino_ratio:.3f}")
    print(f"  Max Drawdown     : {result.max_drawdown_pct:.2f}%  (target < 25%)")
    print(f"  Profit Factor    : {result.profit_factor:.3f}  (target > 1.5)")
    print(f"  SQN              : {result.sqn:.3f}")
    print(separator)
    print(f"  Total Trades     : {result.total_trades}")
    print(f"  Win Rate         : {result.win_rate:.1%}  (target 45-60%)")
    print(f"  Avg Trade PnL    : ${result.avg_trade_pnl:.2f}")
    print(f"{'═' * 52}\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def _run_backtest(args: argparse.Namespace) -> None:
    from binance import AsyncClient
    from core.config import get_secrets

    cfg = load_config(args.config)
    setup_logger(cfg.logging.level, cfg.logging.log_dir)

    secrets = get_secrets()
    client = await AsyncClient.create(
        api_key=secrets.binance_api_key,
        api_secret=secrets.binance_api_secret,
        testnet=cfg.exchange.testnet,
    )

    fetcher = DataFetcher(client, cfg)

    symbol = args.symbol
    interval_entry = cfg.timeframes.entry
    interval_trend = cfg.timeframes.trend
    start = cfg.backtest.start_date
    end = cfg.backtest.end_date

    logger.info(f"Downloading {symbol} {interval_entry} from {start} to {end}…")
    raw_entry = await fetcher.get_klines(symbol, interval_entry, limit=1000,
                                          start_str=start, end_str=end)
    raw_trend = await fetcher.get_klines(symbol, interval_trend, limit=1000,
                                          start_str=start, end_str=end)
    await client.close_connection()

    s = cfg.strategy
    logger.info("Computing indicators…")
    entry_df = compute_all_indicators(
        raw_entry,
        ema_fast=s.ema_fast, ema_slow=s.ema_slow,
        supertrend_period=s.supertrend_period,
        supertrend_multiplier=s.supertrend_multiplier,
        rsi_period=s.rsi_period,
        macd_fast=s.macd_fast, macd_slow=s.macd_slow, macd_signal=s.macd_signal,
        atr_period=cfg.risk.atr_period,
        volume_sma_period=s.volume_sma_period,
        pullback_ema=s.pullback_ema,
        use_supertrend=s.use_supertrend,
    )
    trend_df = compute_all_indicators(
        raw_trend,
        ema_fast=s.ema_fast, ema_slow=s.ema_slow,
        supertrend_period=s.supertrend_period,
        supertrend_multiplier=s.supertrend_multiplier,
        rsi_period=s.rsi_period,
        macd_fast=s.macd_fast, macd_slow=s.macd_slow, macd_signal=s.macd_signal,
        atr_period=cfg.risk.atr_period,
        volume_sma_period=s.volume_sma_period,
        pullback_ema=s.pullback_ema,
        use_supertrend=s.use_supertrend,
    )
    merged = merge_higher_tf(entry_df, trend_df, s.use_supertrend)

    if args.walk_forward:
        logger.info("Running walk-forward validation…")
        wf_results = walk_forward(
            cfg, entry_df, trend_df, symbol,
            n_splits=cfg.backtest.walk_forward_splits,
            in_sample_pct=cfg.backtest.in_sample_pct,
        )
        wf_df = pd.DataFrame(wf_results)
        print(wf_df.to_string(index=False))
        out_path = Path("data") / f"wf_{symbol}_{interval_entry}.csv"
        out_path.parent.mkdir(exist_ok=True)
        wf_df.to_csv(out_path, index=False)
        print(f"\nWalk-forward results saved to {out_path}")
    else:
        engine = BacktestEngine(
            config=cfg,
            entry_df=merged,
            initial_capital=cfg.backtest.initial_capital,
            commission_pct=cfg.backtest.commission_pct,
            symbol=symbol,
        )
        result = engine.run()
        print_results(result)

        # Save equity curve
        out_path = Path("data") / f"equity_{symbol}_{interval_entry}.csv"
        out_path.parent.mkdir(exist_ok=True)
        result.equity_curve.to_csv(out_path, header=["equity"])
        print(f"Equity curve saved to {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest engine")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--walk-forward", action="store_true")
    args = parser.parse_args()
    asyncio.run(_run_backtest(args))
