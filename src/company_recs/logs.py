"""Structured logging: one JSON object per line, via structlog."""

import logging

import structlog

from company_recs.config import LogLevel


def configure_logging(level: LogLevel) -> None:
    """Send JSON log lines at `level` and above to stdout."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        cache_logger_on_first_use=True,
    )
