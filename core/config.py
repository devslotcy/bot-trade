"""
core/config.py
==============
Configuration loading, validation (Pydantic v2), and env-var override.

Priority: env vars > config.yaml defaults.
API keys/secrets must NEVER appear in config.yaml — only via env vars.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class ExchangeConfig(BaseModel):
    testnet: bool = True
    mode: Literal["spot", "futures"] = "spot"
    max_leverage: int = Field(3, ge=1, le=10)
    recv_window: int = 5000
    request_timeout: int = 10


class TimeframeConfig(BaseModel):
    entry: str = "1h"
    trend: str = "4h"


class StrategyConfig(BaseModel):
    name: str = "TrendMomentumHybrid"
    ema_fast: int = 50
    ema_slow: int = 200
    supertrend_period: int = 10
    supertrend_multiplier: float = 3.0
    use_supertrend: bool = False
    pullback_ema: int = 20
    rsi_period: int = 14
    rsi_long_min: float = 40.0
    rsi_short_max: float = 60.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    volume_sma_period: int = 20
    volume_multiplier: float = 1.3
    funding_rate_long_max: float = 0.0001
    funding_rate_short_min: float = 0.0001

    @field_validator("ema_slow")
    @classmethod
    def ema_slow_gt_fast(cls, v: int, info) -> int:
        fast = info.data.get("ema_fast", 50)
        if v <= fast:
            raise ValueError("ema_slow must be greater than ema_fast")
        return v


class RiskConfig(BaseModel):
    risk_per_trade: float = Field(0.0075, gt=0, le=0.02)
    atr_period: int = 14
    sl_atr_multiplier: float = Field(1.2, gt=0)
    tp_r_ratio: float = Field(3.0, gt=1)
    partial_tp_r: float = Field(1.5, gt=0)
    partial_tp_pct: float = Field(0.50, gt=0, le=1)
    trailing_stop_activation_r: float = 1.2
    trailing_stop_atr_multiplier: float = 1.2
    max_concurrent_positions: int = Field(3, ge=1, le=10)
    daily_loss_limit: float = Field(-0.02, lt=0)
    loss_cooldown_hours: float = Field(4.0, ge=0)
    max_hold_hours: float = Field(72.0, gt=0)
    slippage_guard_pct: float = Field(0.003, gt=0)


class ExecutionConfig(BaseModel):
    use_limit_orders: bool = True
    limit_order_timeout_s: int = 10
    max_retries: int = 3
    retry_delay_s: float = 2.0


class ReliabilityConfig(BaseModel):
    heartbeat_interval_s: int = 60
    circuit_breaker_failures: int = 5
    circuit_breaker_pause_min: int = 20
    ws_reconnect_delay_s: float = 5.0
    ws_max_reconnect_attempts: int = 10


class NotificationsConfig(BaseModel):
    telegram_enabled: bool = True
    daily_summary_utc_hour: int = Field(0, ge=0, le=23)


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_dir: str = "logs"
    rotate_size_mb: int = 50
    retention_days: int = 30


class DatabaseConfig(BaseModel):
    path: str = "data/trades.db"


class BacktestConfig(BaseModel):
    start_date: str = "2020-01-01"
    end_date: str = "2025-12-31"
    initial_capital: float = 10000.0
    commission_pct: float = 0.001
    walk_forward_splits: int = 5
    in_sample_pct: float = Field(0.70, gt=0, lt=1)


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

class BotConfig(BaseModel):
    """Fully validated bot configuration."""

    exchange: ExchangeConfig = ExchangeConfig()
    symbols: List[str] = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    timeframes: TimeframeConfig = TimeframeConfig()
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    reliability: ReliabilityConfig = ReliabilityConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    logging: LoggingConfig = LoggingConfig()
    database: DatabaseConfig = DatabaseConfig()
    backtest: BacktestConfig = BacktestConfig()

    @field_validator("symbols")
    @classmethod
    def symbols_not_empty(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("symbols list must not be empty")
        return [s.upper() for s in v]


# ---------------------------------------------------------------------------
# Secrets (env-only, never in YAML)
# ---------------------------------------------------------------------------

class Secrets(BaseSettings):
    """Sensitive credentials — loaded exclusively from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    binance_api_key: str = Field(..., alias="BINANCE_API_KEY")
    binance_api_secret: str = Field(..., alias="BINANCE_API_SECRET")
    telegram_bot_token: Optional[str] = Field(None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = Field(None, alias="TELEGRAM_CHAT_ID")

    model_config = SettingsConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_config(path: str | Path = "config.yaml") -> BotConfig:
    """Load and validate config from YAML file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path) as f:
        raw = yaml.safe_load(f)
    return BotConfig.model_validate(raw or {})


@lru_cache(maxsize=1)
def get_config() -> BotConfig:
    """Cached config singleton."""
    return load_config()


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    """Cached secrets singleton — raises if env vars missing."""
    return Secrets()  # type: ignore[call-arg]
