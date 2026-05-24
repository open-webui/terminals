"""Loguru configuration — replaces stdlib logging across the project."""

import logging
import sys

from loguru import logger

from terminals.config import settings


_VALID_LEVELS = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}


class _InterceptHandler(logging.Handler):
    """Route stdlib logging messages into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        # Find caller from where the logged message originated.
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def _resolve_level() -> str:
    """Resolve and validate the configured log level, falling back to INFO."""
    level = (settings.log_level or "INFO").upper()
    if level not in _VALID_LEVELS:
        logger.warning(
            "Invalid TERMINALS_LOG_LEVEL={!r}; falling back to INFO. "
            "Valid levels: {}",
            settings.log_level,
            ", ".join(sorted(_VALID_LEVELS)),
        )
        return "INFO"
    return level


def setup_logging() -> None:
    """Call once at startup to configure loguru and intercept stdlib logging."""

    level = _resolve_level()

    # Remove default loguru handler and add our own.
    logger.remove()
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        level=level,
        colorize=True,
    )

    # Intercept stdlib logging (uvicorn, sqlalchemy, etc.). We forward at
    # level=0 so loguru — not the stdlib handler — decides what is emitted,
    # keeping the configured TERMINALS_LOG_LEVEL the single source of truth.
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy"):
        named = logging.getLogger(name)
        named.handlers = [_InterceptHandler()]
        named.propagate = False
