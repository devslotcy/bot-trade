"""
core/logger.py
==============
Structured logging using loguru with file rotation and console output.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from loguru import logger as _logger


def setup_logger(
    level: str = "INFO",
    log_dir: str = "logs",
    rotate_size_mb: int = 50,
    retention_days: int = 30,
) -> None:
    """Configure loguru for the bot process.

    Args:
        level: Logging level (DEBUG/INFO/WARNING/ERROR).
        log_dir: Directory for log files.
        rotate_size_mb: Max file size before rotation.
        retention_days: Days to keep rotated files.
    """
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    # Remove default handler
    _logger.remove()

    # Console — coloured, human-readable
    _logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
            "<level>{message}</level>"
        ),
        colorize=True,
        backtrace=True,
        diagnose=True,
    )

    # File — JSON-friendly, rotated
    _logger.add(
        f"{log_dir}/bot_{{time:YYYY-MM-DD}}.log",
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} — {message}",
        rotation=f"{rotate_size_mb} MB",
        retention=f"{retention_days} days",
        compression="gz",
        backtrace=True,
        diagnose=False,  # no variable dumps in file for security
        enqueue=True,    # async-safe write
    )

    # Separate error log
    _logger.add(
        f"{log_dir}/errors.log",
        level="ERROR",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} — {message}",
        rotation="10 MB",
        retention="60 days",
        compression="gz",
        backtrace=True,
        diagnose=True,
        enqueue=True,
    )


# Re-export logger so modules do: from core.logger import logger
logger = _logger
