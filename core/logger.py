"""
core/logger.py

Loguru-based logging setup for Skolem.

Usage anywhere in the codebase:
    from core.logger import logger
    logger.info("something happened")
    logger.error("it broke: {err}", err=e)

Render captures stdout/stderr automatically — check logs in the Render dashboard.
Set LOG_LEVEL=DEBUG in env vars to see verbose output (default: INFO).
"""

import logging
import os
import sys

from loguru import logger

__all__ = ["logger", "setup_logging"]


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()

    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
        backtrace=True,
        diagnose=True,
    )

    # Route Python's standard logging (used by uvicorn, supabase-py, httpx)
    # through loguru so everything appears in one stream.
    class _InterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                level_ = logger.level(record.levelname).name
            except ValueError:
                level_ = record.levelno
            frame, depth = sys._getframe(6), 6
            while frame and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1
            logger.opt(depth=depth, exception=record.exc_info).log(
                level_, record.getMessage()
            )

    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        logging.getLogger(name).handlers = [_InterceptHandler()]
