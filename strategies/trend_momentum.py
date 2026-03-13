"""
strategies/trend_momentum.py
=============================
TrendMomentumHybrid — Multi-timeframe pullback strategy.

Signal generation logic (ALL conditions must align):
────────────────────────────────────────────────────
LONG entry:
  1. Higher-TF trend = UP  (EMA50 > EMA200  OR SuperTrend dir = +1)
  2. Entry-TF: price pulled back to pb_ema (close ≤ pb_ema × 1.005)
  3. RSI > rsi_long_min and rising (vs prior bar)
  4. MACD histogram > 0 and crossed from negative (or still positive trending)
  5. Volume > volume_sma × volume_multiplier
  6. (Futures) Funding rate < funding_rate_long_max

SHORT entry (futures only):
  Mirror conditions with trend = DOWN.

Exit logic is handled by the RiskManager / Execution layer, not here.
This module only produces SignalEvent objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd
import numpy as np

from core.config import BotConfig
from core.logger import logger
from core.state import PositionSide


class SignalType(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


@dataclass
class SignalEvent:
    """Output of strategy evaluation for one symbol."""

    symbol: str
    signal: SignalType
    entry_price: float           # Suggested limit entry price
    atr: float                   # ATR at signal time (for SL/TP sizing)
    reason: str                  # Human-readable explanation
    funding_rate: float = 0.0    # Funding rate at signal time (futures)


class TrendMomentumStrategy:
    """
    Stateless strategy — evaluates the latest bar of prepared indicator DataFrames.

    Usage:
        strategy = TrendMomentumStrategy(config)
        event = strategy.evaluate(symbol, entry_df, funding_rate)
    """

    def __init__(self, config: BotConfig) -> None:
        self._cfg = config
        self._s = config.strategy
        self._mode = config.exchange.mode

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(
        self,
        symbol: str,
        entry_df: pd.DataFrame,
        funding_rate: float = 0.0,
    ) -> SignalEvent:
        """
        Evaluate the latest fully-closed bar and return a SignalEvent.

        Args:
            symbol:       Trading pair.
            entry_df:     Entry-TF DataFrame with all indicators + 'htf_trend' column.
            funding_rate: Current perpetual funding rate (0 if spot).

        Returns:
            SignalEvent with LONG/SHORT/NONE signal.
        """
        if len(entry_df) < 3:
            return SignalEvent(symbol, SignalType.NONE, 0.0, 0.0, "Insufficient data")

        bar = entry_df.iloc[-1]       # most recent closed bar
        prev = entry_df.iloc[-2]      # one bar before

        atr_col = f"atr_{self._s.atr_period if hasattr(self._s, 'atr_period') else 14}"
        # Fallback: use atr_14
        atr_col = atr_col if atr_col in entry_df.columns else "atr_14"
        atr = float(bar.get(atr_col, 0.0))
        if atr <= 0:
            return SignalEvent(symbol, SignalType.NONE, 0.0, 0.0, "ATR is zero")

        rsi_col = f"rsi_{self._s.rsi_period}"
        rsi = float(bar.get(rsi_col, 50.0))
        rsi_prev = float(prev.get(rsi_col, 50.0))
        pb_ema_col = f"pb_ema_{self._s.pullback_ema}"
        pb_ema = float(bar.get(pb_ema_col, 0.0))
        volume_sma_col = f"volume_sma_{self._s.volume_sma_period}"
        volume_sma = float(bar.get(volume_sma_col, 0.0))
        close = float(bar["close"])
        volume = float(bar["volume"])
        macd_hist = float(bar.get("macd_hist", 0.0))
        macd_hist_prev = float(prev.get("macd_hist", 0.0))
        htf_trend = int(bar.get("htf_trend", 0))

        # ── LONG signal ───────────────────────────────────────────────────────
        long_conditions = self._check_long(
            htf_trend, close, pb_ema, rsi, rsi_prev,
            macd_hist, macd_hist_prev, volume, volume_sma, funding_rate
        )
        if long_conditions[0]:
            return SignalEvent(
                symbol=symbol,
                signal=SignalType.LONG,
                entry_price=close,
                atr=atr,
                reason=long_conditions[1],
                funding_rate=funding_rate,
            )

        # ── SHORT signal (futures only) ────────────────────────────────────────
        if self._mode == "futures":
            short_conditions = self._check_short(
                htf_trend, close, pb_ema, rsi, rsi_prev,
                macd_hist, macd_hist_prev, volume, volume_sma, funding_rate
            )
            if short_conditions[0]:
                return SignalEvent(
                    symbol=symbol,
                    signal=SignalType.SHORT,
                    entry_price=close,
                    atr=atr,
                    reason=short_conditions[1],
                    funding_rate=funding_rate,
                )

        return SignalEvent(symbol, SignalType.NONE, 0.0, 0.0, "No signal")

    # ── Condition checkers ────────────────────────────────────────────────────

    def _check_long(
        self,
        htf_trend: int,
        close: float,
        pb_ema: float,
        rsi: float,
        rsi_prev: float,
        macd_hist: float,
        macd_hist_prev: float,
        volume: float,
        volume_sma: float,
        funding_rate: float,
    ) -> tuple[bool, str]:
        """Return (True, reason) if all long conditions are met."""
        fails = []

        # 1. Higher-TF uptrend
        if htf_trend != 1:
            fails.append("HTF not uptrend")

        # 2. Price at or near pullback EMA (within 0.5% above)
        if pb_ema > 0 and close > pb_ema * 1.005:
            fails.append(f"Not at pullback EMA (close={close:.2f} > pb_ema={pb_ema:.2f}*1.005)")

        # 3. RSI > threshold and rising
        if rsi < self._s.rsi_long_min:
            fails.append(f"RSI {rsi:.1f} < {self._s.rsi_long_min}")
        if rsi <= rsi_prev:
            fails.append(f"RSI not rising ({rsi:.1f} <= {rsi_prev:.1f})")

        # 4. MACD histogram positive (momentum confirmation)
        if macd_hist <= 0:
            fails.append(f"MACD hist not positive ({macd_hist:.4f})")

        # 5. Volume spike
        if volume_sma > 0 and volume < volume_sma * self._s.volume_multiplier:
            fails.append(f"Volume too low ({volume:.0f} < {volume_sma:.0f}×{self._s.volume_multiplier})")

        # 6. Funding rate (futures only)
        if self._mode == "futures" and funding_rate > self._s.funding_rate_long_max:
            fails.append(f"Funding too high for long ({funding_rate:.5f})")

        if not fails:
            return True, "LONG: all conditions met"
        return False, "; ".join(fails)

    def _check_short(
        self,
        htf_trend: int,
        close: float,
        pb_ema: float,
        rsi: float,
        rsi_prev: float,
        macd_hist: float,
        macd_hist_prev: float,
        volume: float,
        volume_sma: float,
        funding_rate: float,
    ) -> tuple[bool, str]:
        """Return (True, reason) if all short conditions are met."""
        fails = []

        # 1. Higher-TF downtrend
        if htf_trend != -1:
            fails.append("HTF not downtrend")

        # 2. Price at or near pullback EMA (within 0.5% below — rally into EMA)
        if pb_ema > 0 and close < pb_ema * 0.995:
            fails.append(f"Not at pullback EMA (close={close:.2f} < pb_ema={pb_ema:.2f}*0.995)")

        # 3. RSI < threshold and falling
        if rsi > self._s.rsi_short_max:
            fails.append(f"RSI {rsi:.1f} > {self._s.rsi_short_max}")
        if rsi >= rsi_prev:
            fails.append(f"RSI not falling ({rsi:.1f} >= {rsi_prev:.1f})")

        # 4. MACD histogram negative (downward momentum)
        if macd_hist >= 0:
            fails.append(f"MACD hist not negative ({macd_hist:.4f})")

        # 5. Volume spike
        if volume_sma > 0 and volume < volume_sma * self._s.volume_multiplier:
            fails.append(f"Volume too low")

        # 6. Funding rate — short when funding > threshold (longs getting squeezed)
        if funding_rate < self._s.funding_rate_short_min:
            fails.append(f"Funding too low for short ({funding_rate:.5f})")

        if not fails:
            return True, "SHORT: all conditions met"
        return False, "; ".join(fails)

    def should_exit_time(self, age_hours: float) -> bool:
        """Return True if position exceeded max hold time."""
        from core.config import get_config
        cfg = get_config()
        return age_hours >= cfg.risk.max_hold_hours

    def check_trailing_stop(
        self,
        current_price: float,
        entry_price: float,
        side: PositionSide,
        atr: float,
        trailing_active: bool,
        trailing_price: float,
        sl_atr_mult: float,
        trail_atr_mult: float,
        activation_r: float,
        sl_price: float,
    ) -> tuple[bool, float]:
        """
        Determine if trailing stop should activate or update.

        Returns:
            (trailing_active, new_trailing_stop_price)
        """
        r_distance = abs(entry_price - sl_price)  # 1R in price
        if r_distance == 0:
            return trailing_active, trailing_price

        if side == PositionSide.LONG:
            profit_r = (current_price - entry_price) / r_distance
            if profit_r >= activation_r:
                new_trail = current_price - trail_atr_mult * atr
                updated = max(trailing_price, new_trail) if trailing_active else new_trail
                return True, updated
        else:
            profit_r = (entry_price - current_price) / r_distance
            if profit_r >= activation_r:
                new_trail = current_price + trail_atr_mult * atr
                updated = min(trailing_price, new_trail) if trailing_active else new_trail
                return True, updated

        return trailing_active, trailing_price
