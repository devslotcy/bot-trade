"""
data/indicators.py
==================
Pure-function technical indicator calculations using pandas / pandas_ta.

All functions accept a DataFrame with OHLCV columns and return a new
DataFrame (never mutate in-place) with indicator columns appended.
This keeps the data pipeline functional and testable.
"""

from __future__ import annotations

import pandas as pd
import numpy as np
import pandas_ta as ta


# ---------------------------------------------------------------------------
# EMA Cloud (trend filter)
# ---------------------------------------------------------------------------

def add_ema_cloud(df: pd.DataFrame, fast: int = 50, slow: int = 200) -> pd.DataFrame:
    """
    Append EMA-fast and EMA-slow columns.
    Trend direction: ema_fast > ema_slow → uptrend (+1), else downtrend (-1).
    """
    out = df.copy()
    out[f"ema_{fast}"] = ta.ema(out["close"], length=fast)
    out[f"ema_{slow}"] = ta.ema(out["close"], length=slow)
    out["ema_trend"] = np.where(out[f"ema_{fast}"] > out[f"ema_{slow}"], 1, -1)
    return out


# ---------------------------------------------------------------------------
# SuperTrend (trend filter alternative)
# ---------------------------------------------------------------------------

def add_supertrend(
    df: pd.DataFrame, period: int = 10, multiplier: float = 3.0
) -> pd.DataFrame:
    """
    Append SuperTrend columns.
    'supertrend_dir': 1 = uptrend, -1 = downtrend.
    """
    out = df.copy()
    st = ta.supertrend(out["high"], out["low"], out["close"], length=period, multiplier=multiplier)
    if st is not None and not st.empty:
        # pandas_ta returns columns like SUPERT_10_3.0, SUPERTd_10_3.0
        dir_col = [c for c in st.columns if c.startswith("SUPERTd")][0]
        val_col = [c for c in st.columns if c.startswith("SUPERT_")][0]
        out["supertrend"] = st[val_col]
        out["supertrend_dir"] = st[dir_col]   # 1 or -1
    else:
        out["supertrend"] = np.nan
        out["supertrend_dir"] = 0
    return out


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Append ATR column."""
    out = df.copy()
    out[f"atr_{period}"] = ta.atr(out["high"], out["low"], out["close"], length=period)
    return out


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

def add_rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Append RSI column."""
    out = df.copy()
    out[f"rsi_{period}"] = ta.rsi(out["close"], length=period)
    return out


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

def add_macd(
    df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """Append MACD, MACD signal, and MACD histogram columns."""
    out = df.copy()
    macd = ta.macd(out["close"], fast=fast, slow=slow, signal=signal)
    if macd is not None and not macd.empty:
        out["macd"] = macd.iloc[:, 0]
        out["macd_hist"] = macd.iloc[:, 1]
        out["macd_signal"] = macd.iloc[:, 2]
    else:
        out["macd"] = out["macd_hist"] = out["macd_signal"] = np.nan
    return out


# ---------------------------------------------------------------------------
# Volume SMA
# ---------------------------------------------------------------------------

def add_volume_sma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """Append rolling volume SMA."""
    out = df.copy()
    out[f"volume_sma_{period}"] = out["volume"].rolling(period).mean()
    return out


# ---------------------------------------------------------------------------
# Pullback EMA (entry timeframe)
# ---------------------------------------------------------------------------

def add_pullback_ema(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """Append the EMA used for pullback detection."""
    out = df.copy()
    out[f"pb_ema_{period}"] = ta.ema(out["close"], length=period)
    return out


# ---------------------------------------------------------------------------
# All-in-one: compute every indicator needed by the strategy
# ---------------------------------------------------------------------------

def compute_all_indicators(
    df: pd.DataFrame,
    *,
    ema_fast: int = 50,
    ema_slow: int = 200,
    supertrend_period: int = 10,
    supertrend_multiplier: float = 3.0,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    atr_period: int = 14,
    volume_sma_period: int = 20,
    pullback_ema: int = 20,
    use_supertrend: bool = False,
) -> pd.DataFrame:
    """
    Apply all required indicators in one pass.
    Returns enriched DataFrame; original is not mutated.
    Drops leading rows that have NaN in critical columns.
    """
    out = df.copy()
    out = add_ema_cloud(out, fast=ema_fast, slow=ema_slow)
    if use_supertrend:
        out = add_supertrend(out, period=supertrend_period, multiplier=supertrend_multiplier)
    out = add_atr(out, period=atr_period)
    out = add_rsi(out, period=rsi_period)
    out = add_macd(out, fast=macd_fast, slow=macd_slow, signal=macd_signal)
    out = add_volume_sma(out, period=volume_sma_period)
    out = add_pullback_ema(out, period=pullback_ema)

    # Drop warm-up rows where critical indicators are NaN
    required = [f"ema_{ema_slow}", f"rsi_{rsi_period}", "macd_hist", f"atr_{atr_period}"]
    out.dropna(subset=required, inplace=True)
    return out


# ---------------------------------------------------------------------------
# Merge higher-TF trend into entry-TF DataFrame
# ---------------------------------------------------------------------------

def merge_higher_tf(
    entry_df: pd.DataFrame,
    trend_df: pd.DataFrame,
    use_supertrend: bool = False,
) -> pd.DataFrame:
    """
    Forward-fill higher-timeframe trend direction into the entry DataFrame.

    Args:
        entry_df:  1h (entry TF) DataFrame with DatetimeIndex.
        trend_df:  4h (trend TF) DataFrame with DatetimeIndex.
        use_supertrend: Use supertrend_dir column instead of ema_trend.

    Returns:
        entry_df with 'htf_trend' column (1=up, -1=down).
    """
    col = "supertrend_dir" if use_supertrend else "ema_trend"
    trend_series = trend_df[col].rename("htf_trend")

    # Reindex to entry TF timestamps, then forward-fill
    combined = entry_df.copy()
    combined["htf_trend"] = trend_series.reindex(combined.index, method="ffill")
    return combined
