"""Loguru configuration — replaces stdlib logging across the project."""

import logging
import sys

from loguru import logger

from terminals.config import settings

_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
LOG_LEVEL_NAMES = tuple(_LOG_LEVELS)


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


def normalize_log_level(value: str | None, default: str = "INFO") -> str:
    """Return a supported logging level name."""
    level = (value or default).strip().upper()
    return level if level in _LOG_LEVELS else default


def setup_logging() -> None:
    """Call once at startup to configure loguru and intercept stdlib logging."""
    level = normalize_log_level(settings.log_level)

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
    if level != (settings.log_level or "").strip().upper():
        logger.warning(
            "Invalid TERMINALS_LOG_LEVEL={!r}; using INFO. Expected one of: {}",
            settings.log_level,
            ", ".join(LOG_LEVEL_NAMES),
        )

    # Intercept stdlib logging (uvicorn, sqlalchemy, etc.).
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy"):
        named = logging.getLogger(name)
        named.handlers = [_InterceptHandler()]
        named.setLevel(0)
        named.propagate = False
