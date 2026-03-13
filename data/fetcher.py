"""
data/fetcher.py
===============
Async data layer — REST historical OHLCV download + WebSocket live klines.

Design:
  • DataFetcher.get_klines()        → historical DataFrame (REST)
  • KlineStreamManager              → one WS per symbol/timeframe, callbacks
  • On WS disconnect → auto-reconnect with exponential back-off
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
from binance import AsyncClient, BinanceSocketManager
from binance.exceptions import BinanceAPIException
from tenacity import (
    AsyncRetrying,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from core.config import BotConfig, Secrets
from core.logger import logger


# Binance kline column names
_KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]

_NUMERIC_COLS = ["open", "high", "low", "close", "volume", "quote_volume"]


def _parse_klines(raw: List[List]) -> pd.DataFrame:
    """Convert raw Binance kline list → typed DataFrame."""
    df = pd.DataFrame(raw, columns=_KLINE_COLS)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df.set_index("open_time", inplace=True)
    for col in _NUMERIC_COLS:
        df[col] = df[col].astype(float)
    df["trades"] = df["trades"].astype(int)
    return df[_NUMERIC_COLS + ["trades"]]


class DataFetcher:
    """
    Fetches historical OHLCV from Binance REST API.
    Handles pagination (>1000 bars), retries on transient errors.
    """

    def __init__(self, client: AsyncClient, config: BotConfig) -> None:
        self._client = client
        self._cfg = config

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_str: Optional[str] = None,
        end_str: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        Fetch historical klines with automatic pagination.

        Args:
            symbol:    Trading pair, e.g. "BTCUSDT".
            interval:  Binance interval string, e.g. "1h", "4h".
            limit:     Bars per page (max 1000).
            start_str: ISO date string or Binance ms timestamp.
            end_str:   ISO date string or Binance ms timestamp.

        Returns:
            DataFrame indexed by open_time (UTC).
        """
        all_frames: List[pd.DataFrame] = []
        params: Dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": min(limit, 1000),
        }
        if start_str:
            params["startTime"] = start_str
        if end_str:
            params["endTime"] = end_str

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._cfg.execution.max_retries),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            retry=retry_if_exception_type((BinanceAPIException, asyncio.TimeoutError)),
        ):
            with attempt:
                raw = await self._client.get_klines(**params)

        if not raw:
            return pd.DataFrame()

        df = _parse_klines(raw)
        all_frames.append(df)

        # Paginate if start/end provided and we may have more data
        while start_str and end_str and len(raw) == 1000:
            last_ts = int(df.index[-1].timestamp() * 1000) + 1
            params["startTime"] = last_ts
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=2, max=15),
                retry=retry_if_exception_type((BinanceAPIException, asyncio.TimeoutError)),
            ):
                with attempt:
                    raw = await self._client.get_klines(**params)
            if not raw:
                break
            df = _parse_klines(raw)
            all_frames.append(df)

        result = pd.concat(all_frames) if len(all_frames) > 1 else all_frames[0]
        result = result[~result.index.duplicated(keep="last")]
        result.sort_index(inplace=True)
        logger.debug(f"Fetched {len(result)} klines for {symbol} {interval}")
        return result

    async def get_account_balance(self, asset: str = "USDT") -> float:
        """Return free balance of given asset."""
        if self._cfg.exchange.mode == "futures":
            info = await self._client.futures_account_balance()
            for item in info:
                if item["asset"] == asset:
                    return float(item["balance"])
            return 0.0
        else:
            info = await self._client.get_asset_balance(asset=asset)
            return float(info["free"]) if info else 0.0

    async def get_symbol_info(self, symbol: str) -> Dict:
        """Fetch symbol trading rules (lot size, min notional, tick size)."""
        info = await self._client.get_symbol_info(symbol)
        return info or {}

    async def get_funding_rate(self, symbol: str) -> float:
        """Fetch latest perpetual funding rate (futures only)."""
        try:
            data = await self._client.futures_funding_rate(symbol=symbol, limit=1)
            return float(data[-1]["fundingRate"]) if data else 0.0
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# WebSocket stream manager
# ---------------------------------------------------------------------------

KlineCallback = Callable[[str, str, pd.Series], None]


class KlineStreamManager:
    """
    Manages Binance WebSocket kline streams.

    Calls `on_kline(symbol, interval, bar)` for every closed kline,
    where bar is a pandas Series with OHLCV fields.
    Auto-reconnects on failure.
    """

    def __init__(
        self,
        client: AsyncClient,
        config: BotConfig,
        on_kline: KlineCallback,
    ) -> None:
        self._client = client
        self._cfg = config
        self._on_kline = on_kline
        self._tasks: List[asyncio.Task] = []
        self._running = False

    async def start(self, symbols: List[str], intervals: List[str]) -> None:
        """Start one listener task per symbol+interval combination."""
        self._running = True
        for symbol in symbols:
            for interval in intervals:
                task = asyncio.create_task(
                    self._listen(symbol, interval),
                    name=f"ws_{symbol}_{interval}",
                )
                self._tasks.append(task)
        logger.info(f"WebSocket streams started: {symbols} × {intervals}")

    async def stop(self) -> None:
        """Cancel all stream tasks gracefully."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("WebSocket streams stopped")

    async def _listen(self, symbol: str, interval: str) -> None:
        """Single symbol/interval listener with reconnect logic."""
        delay = self._cfg.reliability.ws_reconnect_delay_s
        max_attempts = self._cfg.reliability.ws_max_reconnect_attempts
        attempt = 0

        while self._running and attempt <= max_attempts:
            try:
                bsm = BinanceSocketManager(self._client)
                stream = bsm.kline_socket(symbol=symbol, interval=interval)
                async with stream as ws:
                    attempt = 0  # reset on successful connect
                    logger.info(f"WS connected: {symbol} {interval}")
                    async for msg in ws:
                        if not self._running:
                            return
                        self._handle_message(symbol, interval, msg)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                attempt += 1
                wait = min(delay * (2 ** attempt), 60)
                logger.warning(
                    f"WS {symbol}/{interval} error (attempt {attempt}): {exc} — "
                    f"retrying in {wait:.0f}s"
                )
                await asyncio.sleep(wait)

        if attempt > max_attempts:
            logger.error(f"WS {symbol}/{interval} exceeded max reconnect attempts")

    def _handle_message(self, symbol: str, interval: str, msg: Dict) -> None:
        """Parse raw WS message and invoke callback on closed klines."""
        try:
            k = msg.get("data", msg).get("k", {})
            if not k.get("x"):       # x=True means kline is closed
                return
            bar = pd.Series(
                {
                    "open": float(k["o"]),
                    "high": float(k["h"]),
                    "low": float(k["l"]),
                    "close": float(k["c"]),
                    "volume": float(k["v"]),
                    "trades": int(k["n"]),
                },
                name=pd.Timestamp(k["t"], unit="ms", tz="UTC"),
            )
            self._on_kline(symbol, interval, bar)
        except Exception as exc:
            logger.error(f"WS message parse error [{symbol}/{interval}]: {exc}")
