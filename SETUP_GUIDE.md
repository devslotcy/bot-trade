# Crypto Trading Bot — Setup & Security Guide

## Table of Contents
1. [Prerequisites](#prerequisites)
2. [Local Setup](#local-setup)
3. [API Key Security](#api-key-security)
4. [Binance Testnet First](#binance-testnet-first)
5. [Running the Bot](#running-the-bot)
6. [Running the Dashboard](#running-the-dashboard)
7. [Backtesting](#backtesting)
8. [VPS Deployment](#vps-deployment)
9. [Monitoring & Alerts](#monitoring--alerts)

---

## Prerequisites

- Python 3.11+
- Binance account (Spot or Futures)
- Telegram bot (optional but strongly recommended)
- VPS with 1 GB RAM minimum (Ubuntu 22.04 recommended for production)

---

## Local Setup

```bash
# 1. Clone / copy project
cd bot-trade

# 2. Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Create your .env file (NEVER commit this)
cp .env.example .env
# Edit .env with your real keys

# 5. Create data and logs directories
mkdir -p data logs
```

---

## API Key Security

### Creating Binance API Keys

1. Log in to Binance → Profile → API Management.
2. Create a new API key — label it clearly (e.g., "TradingBot-VPS").
3. **Permissions to enable:**
   - Enable Reading
   - Enable Spot & Margin Trading (for spot mode)
   - Enable Futures (only if using futures mode)
4. **Permissions to DISABLE:**
   - Withdrawals — absolutely OFF
   - Universal Transfer — OFF
5. **IP Whitelist:** Restrict to your VPS IP address. This is critical.

### Storing Secrets Safely

```bash
# .env file (local dev)
BINANCE_API_KEY=abc123...
BINANCE_API_SECRET=xyz789...
TELEGRAM_BOT_TOKEN=111:AAA...
TELEGRAM_CHAT_ID=123456789
```

**Rules:**
- `.env` is in `.gitignore` — never commit it.
- On VPS, use environment variables in systemd service file or export in shell.
- Never hardcode keys in any Python file.
- Rotate keys every 90 days.

---

## Binance Testnet First

**Always validate on testnet before going live.**

1. Register at https://testnet.binance.vision (Spot Testnet) or
   https://testnet.binancefuture.com (Futures Testnet).
2. Generate testnet API keys from the testnet dashboard.
3. Set in `.env`:
   ```
   BINANCE_API_KEY=<testnet_key>
   BINANCE_API_SECRET=<testnet_secret>
   ```
4. In `config.yaml` ensure:
   ```yaml
   exchange:
     testnet: true
   ```
5. Run the bot and verify:
   - Orders appear in testnet dashboard
   - Equity updates correctly
   - Telegram notifications arrive
   - No errors in `logs/`

---

## Running the Bot

### Development (foreground)

```bash
source .venv/bin/activate

# Dry-run (signals only, no orders)
python main.py --dry-run

# Live on testnet
python main.py

# With custom config
python main.py --config config.yaml
```

### Production (background with logging)

```bash
nohup python main.py >> logs/nohup.out 2>&1 &
echo $! > bot.pid
```

To stop:
```bash
kill $(cat bot.pid)
```

---

## Running the Dashboard

```bash
source .venv/bin/activate
streamlit run dashboard.py --server.port 8501 --server.address 0.0.0.0
```

Open in browser: `http://localhost:8501` (or your VPS IP).

> **Security:** On VPS, bind to localhost and use nginx reverse proxy with basic auth or SSH tunnel.

SSH tunnel from local machine:
```bash
ssh -L 8501:localhost:8501 user@your-vps-ip
# Then open http://localhost:8501 locally
```

---

## Backtesting

### Full backtest (single symbol)

```bash
python -m backtest.engine --symbol BTCUSDT --config config.yaml
```

### Walk-forward validation

```bash
python -m backtest.engine --symbol BTCUSDT --walk-forward
```

Results are printed to console and saved to:
- `data/equity_BTCUSDT_1h.csv` — equity curve
- `data/wf_BTCUSDT_1h.csv` — walk-forward fold metrics

### Interpreting results

| Metric | Target | Description |
|---|---|---|
| Sharpe Ratio | > 1.2 | Risk-adjusted return |
| Max Drawdown | < 25% | Worst peak-to-trough |
| Profit Factor | > 1.5 | Gross profit / gross loss |
| Win Rate | 45–60% | % of winning trades |
| SQN | > 2.0 | System Quality Number |

---

## VPS Deployment

### Ubuntu 22.04 setup

```bash
# Update system
sudo apt update && sudo apt upgrade -y
sudo apt install python3.11 python3.11-venv python3-pip git -y

# Clone project
git clone <your-repo> ~/bot-trade
cd ~/bot-trade

# Setup venv
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create .env with production keys
nano .env

# Create directories
mkdir -p data logs
```

### systemd service (recommended for 24/7 uptime)

Create `/etc/systemd/system/tradingbot.service`:

```ini
[Unit]
Description=Crypto Trading Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/bot-trade
Environment=PATH=/home/ubuntu/bot-trade/.venv/bin
EnvironmentFile=/home/ubuntu/bot-trade/.env
ExecStart=/home/ubuntu/bot-trade/.venv/bin/python main.py
Restart=on-failure
RestartSec=30
StandardOutput=append:/home/ubuntu/bot-trade/logs/systemd.log
StandardError=append:/home/ubuntu/bot-trade/logs/systemd_err.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable tradingbot
sudo systemctl start tradingbot
sudo systemctl status tradingbot

# View logs
sudo journalctl -u tradingbot -f
```

### Dashboard as service

Create `/etc/systemd/system/botdashboard.service`:

```ini
[Unit]
Description=Bot Streamlit Dashboard
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/bot-trade
Environment=PATH=/home/ubuntu/bot-trade/.venv/bin
EnvironmentFile=/home/ubuntu/bot-trade/.env
ExecStart=/home/ubuntu/bot-trade/.venv/bin/streamlit run dashboard.py --server.port 8501 --server.address 127.0.0.1
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable botdashboard
sudo systemctl start botdashboard
```

### nginx reverse proxy with basic auth

```bash
sudo apt install nginx apache2-utils -y
sudo htpasswd -c /etc/nginx/.htpasswd botuser
```

`/etc/nginx/sites-available/botdashboard`:
```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    location / {
        auth_basic "Bot Dashboard";
        auth_basic_user_file /etc/nginx/.htpasswd;
        proxy_pass http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
```

---

## Monitoring & Alerts

### Telegram Bot Setup

1. Message `@BotFather` on Telegram → `/newbot` → follow prompts.
2. Copy the bot token to `.env` as `TELEGRAM_BOT_TOKEN`.
3. Start a conversation with your bot → get your chat ID:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```
   Look for `"chat":{"id":123456789}` in the response.
4. Set `TELEGRAM_CHAT_ID=123456789` in `.env`.

You will receive:
- Trade entry/exit alerts with PnL
- Daily summary at configured UTC hour
- Circuit-breaker and daily-limit notifications
- Critical error alerts

### Log monitoring

```bash
# Tail live log
tail -f logs/bot_$(date +%Y-%m-%d).log

# Errors only
tail -f logs/errors.log

# Systemd journal
journalctl -u tradingbot -f --since "1 hour ago"
```

---

## Architecture Overview

```
bot-trade/
├── main.py               ← Async bot entrypoint
├── dashboard.py          ← Streamlit dashboard
├── config.yaml           ← All non-secret config
├── .env                  ← Secrets (never commit)
├── requirements.txt
├── core/
│   ├── config.py         ← Pydantic config + Secrets
│   ├── state.py          ← Thread-safe shared state
│   └── logger.py         ← Loguru structured logging
├── data/
│   ├── fetcher.py        ← REST + WebSocket data layer
│   └── indicators.py     ← pandas_ta indicator functions
├── strategies/
│   └── trend_momentum.py ← TrendMomentumHybrid strategy
├── risk/
│   └── manager.py        ← Position sizing + risk gates
├── execution/
│   └── order_manager.py  ← LIMIT/MARKET order execution
├── backtest/
│   └── engine.py         ← Vectorized backtest + walk-forward
└── utils/
    ├── telegram.py        ← Telegram notifications
    └── database.py        ← SQLite trade persistence
```

---

## Risk Warnings

- **Cryptocurrency trading involves significant financial risk.**
- Past backtest performance does NOT guarantee future results.
- Start with minimum position sizes and paper-trade extensively.
- Never risk more capital than you can afford to lose.
- Monitor the bot actively, especially in the first weeks.
- The daily loss limit (-2%) and max risk per trade (0.75%) are conservative defaults — do not increase them without understanding the implications.
