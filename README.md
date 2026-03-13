# Crypto Trading Bot

Automated cryptocurrency trading bot for Binance — Spot & Futures. Built with Python, featuring a trend-momentum strategy, full risk management, backtesting engine, and Telegram notifications.

## Features

- **Strategy:** Trend-Momentum Hybrid (EMA cloud, RSI, MACD, volume confirmation)
- **Risk Management:** ATR-based stop loss, partial TP, trailing stop, daily loss limit
- **Multi-pair:** BTCUSDT, ETHUSDT, SOLUSDT (configurable)
- **Timeframes:** 1H entry + 4H trend filter
- **Execution:** Limit orders with market fallback
- **Backtesting:** Walk-forward validation engine
- **Dashboard:** Real-time monitoring via `dashboard.py`
- **Notifications:** Telegram alerts & daily PnL summary
- **Safety:** Testnet mode, circuit breaker, API key via env vars only

## Tech Stack

- Python 3.11+
- Binance API (ccxt / REST + WebSocket)
- SQLite (trade history & equity curve)
- Telegram Bot API

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set environment variables (never hardcode keys)
cp .env.example .env
# Edit .env with your Binance API keys and Telegram token

# 3. Run on testnet first
python main.py
```

> See [SETUP_GUIDE.md](SETUP_GUIDE.md) for full setup, VPS deployment, and security guide.

## Project Structure

```
bot-trade/
├── core/           # Config loader, logger, state management
├── strategies/     # TrendMomentumHybrid strategy
├── data/           # Market data fetcher & indicators
├── execution/      # Order manager (limit/market, retries)
├── risk/           # Position sizing, stop loss, circuit breaker
├── backtest/       # Walk-forward backtesting engine
├── utils/          # Database, Telegram notifications
├── dashboard.py    # Live monitoring dashboard
├── main.py         # Entry point
└── config.yaml     # All parameters (no secrets here)
```

## Security

- API keys are **never** stored in code or `config.yaml`
- Use environment variables: `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `TELEGRAM_BOT_TOKEN`
- Always start with `testnet: true` in `config.yaml`
- Set IP whitelist on your Binance API key

## Risk Warning

This bot is for **educational purposes**. Crypto trading carries significant financial risk. Always test on testnet before using real funds.
