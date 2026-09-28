"""
config.logging
==============

Structured logging configuration based on structlog.
Falls back to standard logging if structlog is not installed.
"""
from __future__ import annotations

import logging
import sys

from config.settings import settings

# Try to import structlog, fall back to standard logging if it's missing
try:
    import structlog
    STRUCTLOG_AVAILABLE = True
except ImportError:
    STRUCTLOG_AVAILABLE = False


def configure_logging() -> None:
    """Configure logging for the entire application."""
    level = getattr(logging, settings.app_log_level, logging.INFO)

    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
        level=level,
    )
    
    if STRUCTLOG_AVAILABLE:
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.dev.ConsoleRenderer(colors=not settings.is_production),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(level),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )


class StructuredLoggerWrapper:
    """Wrapper that allows structlog-style logging with standard logging module."""
    def __init__(self, logger: logging.Logger):
        self._logger = logger
    
    def _format_kwargs(self, kwargs):
        """Format keyword arguments into a string for standard logging."""
        if not kwargs:
            return ""
        return " " + " ".join(f"{k}={v!r}" for k, v in kwargs.items())
    
    def debug(self, msg: str, *args, **kwargs):
        formatted = f"{msg}{self._format_kwargs(kwargs)}"
        self._logger.debug(formatted, *args)
    
    def info(self, msg: str, *args, **kwargs):
        formatted = f"{msg}{self._format_kwargs(kwargs)}"
        self._logger.info(formatted, *args)
    
    def warning(self, msg: str, *args, **kwargs):
        formatted = f"{msg}{self._format_kwargs(kwargs)}"
        self._logger.warning(formatted, *args)
    
    def error(self, msg: str, *args, **kwargs):
        formatted = f"{msg}{self._format_kwargs(kwargs)}"
        self._logger.error(formatted, *args)
    
    def critical(self, msg: str, *args, **kwargs):
        formatted = f"{msg}{self._format_kwargs(kwargs)}"
        self._logger.critical(formatted, *args)
    
    def exception(self, msg: str, *args, **kwargs):
        formatted = f"{msg}{self._format_kwargs(kwargs)}"
        self._logger.exception(formatted, *args)


def get_logger(name: str | None = None) -> logging.Logger | StructuredLoggerWrapper:
    """Return a logger bound to ``name``, uses structlog if available, standard logging otherwise."""
    if STRUCTLOG_AVAILABLE:
        return structlog.get_logger(name)
    else:
        return StructuredLoggerWrapper(logging.getLogger(name or __name__))