# 🤖 Bot-Trade — Automated Crypto Trading Bot

A fully automated cryptocurrency trading bot for Binance, using a trend-momentum strategy with built-in risk management and real-time Telegram alerts.

## Features

- **Trend-Momentum Strategy** — combines EMA crossovers with RSI momentum signals
- **Risk Management** — stop-loss, take-profit, and position sizing built-in
- **Backtesting Engine** — test strategies against historical OHLCV data
- **Telegram Alerts** — real-time trade notifications (open, close, PnL)
- **Multi-pair Ready** — run on any Binance spot trading pair

## Tech Stack

- **Language:** Python
- **Exchange:** Binance API (via `python-binance`)
- **Indicators:** TA-Lib / Pandas-TA
- **Notifications:** Telegram Bot API
- **Data:** Pandas, NumPy

## Strategy Overview

```
EMA 20 crosses EMA 50 (upward) + RSI > 50 → BUY
EMA 20 crosses EMA 50 (downward) + RSI < 50 → SELL
Stop-Loss: 2% | Take-Profit: 4%
```

## Setup

```bash
git clone https://github.com/devslotcy/bot-trade
cd bot-trade
pip install -r requirements.txt
cp .env.example .env
# Add Binance API keys and Telegram credentials
python bot.py
```

## Environment Variables

```env
BINANCE_API_KEY=your_key
BINANCE_SECRET=your_secret
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
TRADING_PAIR=BTCUSDT
```

## Backtesting

```bash
python backtest.py --pair BTCUSDT --start 2024-01-01 --end 2024-12-31
```

---

Built by [Mucahit Tiglioglu](https://github.com/devslotcy)
