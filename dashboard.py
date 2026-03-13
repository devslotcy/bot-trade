"""
dashboard.py
============
Streamlit web dashboard for the trading bot.

Features:
  • Live equity curve (Plotly)
  • Open positions table
  • Trade history table with PnL colouring
  • Key metrics: balance, drawdown, win rate, profit factor
  • Start / Stop / Pause bot controls (writes command to shared state file)
  • Config viewer/editor (edits config.yaml)
  • Candlestick chart with EMA + SuperTrend overlay
  • Auto-refreshes every 10 seconds

Run:
    streamlit run dashboard.py --server.port 8501
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yaml
from streamlit_autorefresh import st_autorefresh

# Ensure project root on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import load_config, BotConfig
from utils.database import Database


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Crypto Bot Dashboard",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Auto-refresh every 10 seconds
st_autorefresh(interval=10_000, key="refresh")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONFIG_PATH = Path("config.yaml")
CMD_FILE = Path(".bot_cmd")  # bot polls this file for start/stop/pause


def _load_config_raw() -> str:
    return CONFIG_PATH.read_text() if CONFIG_PATH.exists() else ""


def _save_config_raw(text: str) -> None:
    CONFIG_PATH.write_text(text)


def _send_command(cmd: str) -> None:
    """Write a command file that main.py polls."""
    CMD_FILE.write_text(cmd)


@st.cache_resource
def _get_db() -> Database:
    try:
        cfg = load_config()
        return Database(cfg.database.path)
    except Exception:
        return Database("data/trades.db")


async def _fetch_trades(db: Database, limit: int = 100) -> List[Dict]:
    return await db.get_trades(limit)


async def _fetch_equity(db: Database) -> List[Dict]:
    return await db.get_equity_curve(2000)


def _run_async(coro) -> any:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Sidebar — Controls
# ---------------------------------------------------------------------------

def render_sidebar() -> None:
    st.sidebar.title(" Bot Control")
    st.sidebar.markdown("---")

    col1, col2, col3 = st.sidebar.columns(3)
    if col1.button("▶ Start", use_container_width=True):
        _send_command("start")
        st.sidebar.success("Start command sent")
    if col2.button("⏸ Pause", use_container_width=True):
        _send_command("pause")
        st.sidebar.warning("Pause command sent")
    if col3.button("⏹ Stop", use_container_width=True):
        _send_command("stop")
        st.sidebar.error("Stop command sent")

    st.sidebar.markdown("---")

    # Bot status indicator
    status_file = Path(".bot_status")
    status = status_file.read_text().strip() if status_file.exists() else "unknown"
    colour_map = {
        "running": "green", "paused": "orange", "stopped": "red",
        "circuit_breaker": "red", "daily_limit_hit": "orange",
    }
    colour = colour_map.get(status, "grey")
    st.sidebar.markdown(
        f"**Status:** :{colour}[{'●'} {status.upper().replace('_', ' ')}]"
    )

    st.sidebar.markdown("---")
    st.sidebar.caption(f"Refreshed: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")


# ---------------------------------------------------------------------------
# Metrics row
# ---------------------------------------------------------------------------

def render_metrics(equity_data: List[Dict], trades: List[Dict]) -> None:
    if not equity_data:
        st.info("No equity data yet. Start the bot to begin trading.")
        return

    eq_df = pd.DataFrame(equity_data)
    eq_df["ts"] = pd.to_datetime(eq_df["ts"])
    eq_df.sort_values("ts", inplace=True)

    current_equity = eq_df["equity"].iloc[-1]
    initial_equity = eq_df["equity"].iloc[0]
    total_return = (current_equity - initial_equity) / initial_equity * 100

    # Drawdown
    roll_max = eq_df["equity"].cummax()
    drawdown_series = (eq_df["equity"] - roll_max) / roll_max * 100
    max_dd = drawdown_series.min()
    current_dd = drawdown_series.iloc[-1]

    # Trade metrics from history
    if trades:
        tr_df = pd.DataFrame(trades)
        wins = tr_df[tr_df["pnl_usdt"] > 0]
        losses = tr_df[tr_df["pnl_usdt"] <= 0]
        win_rate = len(wins) / len(tr_df) if len(tr_df) > 0 else 0
        gross_profit = wins["pnl_usdt"].sum()
        gross_loss = abs(losses["pnl_usdt"].sum())
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
        total_trades = len(tr_df)
    else:
        win_rate = profit_factor = total_trades = 0

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Equity", f"${current_equity:,.2f}", f"{total_return:+.2f}%")
    c2.metric("Max Drawdown", f"{max_dd:.2f}%", f"Current: {current_dd:.2f}%")
    c3.metric("Total Trades", total_trades)
    c4.metric("Win Rate", f"{win_rate:.1%}")
    c5.metric("Profit Factor", f"{profit_factor:.2f}" if profit_factor != float("inf") else "∞")
    c6.metric("Daily PnL", "See chart")


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------

def render_equity_curve(equity_data: List[Dict]) -> None:
    if not equity_data:
        return
    eq_df = pd.DataFrame(equity_data)
    eq_df["ts"] = pd.to_datetime(eq_df["ts"])

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=eq_df["ts"], y=eq_df["equity"],
        mode="lines", name="Equity",
        line=dict(color="#00d4aa", width=2),
        fill="tozeroy", fillcolor="rgba(0,212,170,0.08)",
    ))
    fig.update_layout(
        title="Equity Curve",
        xaxis_title="Time",
        yaxis_title="USDT",
        template="plotly_dark",
        height=320,
        margin=dict(l=40, r=20, t=40, b=40),
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Open positions
# ---------------------------------------------------------------------------

def render_open_positions() -> None:
    st.subheader("Open Positions")
    pos_file = Path(".open_positions.json")
    if not pos_file.exists():
        st.info("No open positions.")
        return
    try:
        positions = json.loads(pos_file.read_text())
        if not positions:
            st.info("No open positions.")
            return
        rows = []
        for p in positions:
            rows.append({
                "Symbol": p.get("symbol", ""),
                "Side": p.get("side", ""),
                "Entry": f"{p.get('entry_price', 0):.4f}",
                "SL": f"{p.get('stop_loss', 0):.4f}",
                "TP": f"{p.get('take_profit', 0):.4f}",
                "Qty": p.get("quantity", 0),
                "Risk USDT": f"{p.get('risk_amount', 0):.2f}",
                "Age (h)": f"{p.get('age_hours', 0):.1f}",
                "Trail Active": "" if p.get("trailing_stop_active") else "",
            })
        df = pd.DataFrame(rows)

        def colour_side(val: str) -> str:
            if val == "LONG":
                return "color: #00d4aa"
            elif val == "SHORT":
                return "color: #ff6b6b"
            return ""

        styled = df.style.map(colour_side, subset=["Side"])
        st.dataframe(styled, use_container_width=True, hide_index=True)
    except Exception as exc:
        st.error(f"Error loading positions: {exc}")


# ---------------------------------------------------------------------------
# Trade history
# ---------------------------------------------------------------------------

def render_trade_history(trades: List[Dict]) -> None:
    st.subheader("Trade History (last 100)")
    if not trades:
        st.info("No trades yet.")
        return
    tr_df = pd.DataFrame(trades)
    tr_df["opened_at"] = pd.to_datetime(tr_df["opened_at"])
    tr_df["closed_at"] = pd.to_datetime(tr_df["closed_at"])
    display_cols = ["symbol", "side", "entry_price", "exit_price",
                    "quantity", "pnl_usdt", "pnl_pct", "exit_reason",
                    "fees_usdt", "closed_at"]
    tr_df = tr_df[[c for c in display_cols if c in tr_df.columns]]
    tr_df["pnl_pct"] = tr_df["pnl_pct"].apply(lambda x: f"{x:.2%}")
    tr_df["pnl_usdt"] = tr_df["pnl_usdt"].apply(lambda x: f"{x:+.2f}")

    def colour_pnl(val: str) -> str:
        try:
            v = float(val.replace("+", ""))
            return "color: #00d4aa" if v >= 0 else "color: #ff6b6b"
        except Exception:
            return ""

    styled = tr_df.style.map(colour_pnl, subset=["pnl_usdt"])
    st.dataframe(styled, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Candlestick chart with indicators
# ---------------------------------------------------------------------------

def render_candlestick(symbol: str = "BTCUSDT") -> None:
    st.subheader(f"Chart — {symbol}")
    chart_file = Path(f".chart_{symbol}.json")
    if not chart_file.exists():
        st.info(f"Awaiting live chart data for {symbol}…")
        return
    try:
        data = json.loads(chart_file.read_text())
        df = pd.DataFrame(data)
        df["time"] = pd.to_datetime(df["time"])

        fig = go.Figure()

        # Candlestick
        fig.add_trace(go.Candlestick(
            x=df["time"], open=df["open"], high=df["high"],
            low=df["low"], close=df["close"], name="Price",
            increasing_line_color="#00d4aa",
            decreasing_line_color="#ff6b6b",
        ))

        # EMA 50 & 200
        if "ema_50" in df.columns:
            fig.add_trace(go.Scatter(x=df["time"], y=df["ema_50"],
                                     name="EMA 50", line=dict(color="#ffa500", width=1)))
        if "ema_200" in df.columns:
            fig.add_trace(go.Scatter(x=df["time"], y=df["ema_200"],
                                     name="EMA 200", line=dict(color="#8888ff", width=1.5)))
        # Pullback EMA
        if "pb_ema_20" in df.columns:
            fig.add_trace(go.Scatter(x=df["time"], y=df["pb_ema_20"],
                                     name="EMA 20", line=dict(color="#ffff00", width=1, dash="dot")))

        fig.update_layout(
            template="plotly_dark",
            height=420,
            xaxis_rangeslider_visible=False,
            margin=dict(l=40, r=20, t=40, b=40),
        )
        st.plotly_chart(fig, use_container_width=True)
    except Exception as exc:
        st.error(f"Chart error: {exc}")


# ---------------------------------------------------------------------------
# Config editor
# ---------------------------------------------------------------------------

def render_config_editor() -> None:
    st.subheader("Config Editor (config.yaml)")
    raw = _load_config_raw()
    edited = st.text_area("Edit config:", value=raw, height=400, key="config_editor")
    if st.button("Save Config"):
        try:
            yaml.safe_load(edited)  # validate YAML before saving
            _save_config_raw(edited)
            st.success("Config saved. Restart bot to apply.")
        except yaml.YAMLError as exc:
            st.error(f"Invalid YAML: {exc}")


# ---------------------------------------------------------------------------
# Main layout
# ---------------------------------------------------------------------------

def main() -> None:
    render_sidebar()

    st.title(" Crypto Trading Bot — Dashboard")
    st.markdown("---")

    db = _get_db()
    trades = _run_async(_fetch_trades(db, limit=100))
    equity_data = _run_async(_fetch_equity(db))

    # Metrics
    render_metrics(equity_data, trades)
    st.markdown("---")

    # Charts row
    col_chart, col_pos = st.columns([2, 1])
    with col_chart:
        render_equity_curve(equity_data)
    with col_pos:
        render_open_positions()

    st.markdown("---")

    # Candlestick
    try:
        cfg = load_config()
        symbol = cfg.symbols[0] if cfg.symbols else "BTCUSDT"
    except Exception:
        symbol = "BTCUSDT"

    tab1, tab2, tab3 = st.tabs(["📈 Chart", "📋 Trade History", "⚙️ Config"])
    with tab1:
        render_candlestick(symbol)
    with tab2:
        render_trade_history(trades)
    with tab3:
        render_config_editor()


if __name__ == "__main__":
    main()
