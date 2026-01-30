"""Centralized logging configuration with request ID propagation."""

import logging
import os
import sys
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler

# Context variable for request ID - propagates through entire request flow
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Environment configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "text")  # "text" or "json"
LOG_FILE = os.getenv("LOG_FILE")  # If set, enable file logging
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", 10 * 1024 * 1024))  # 10MB
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", 5))


class RequestIdFormatter(logging.Formatter):
    """Formatter that injects request_id into all log records."""

    def format(self, record):
        # Ensure request_id is always present
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get()
        return super().format(record)


# Formats include request_id
JSON_FORMAT = (
    '{"time": "%(asctime)s", "level": "%(levelname)s", '
    '"request_id": "%(request_id)s", "logger": "%(name)s", "message": "%(message)s"}'
)
TEXT_FORMAT = "%(asctime)s | %(levelname)-8s | %(request_id)s | %(name)s | %(message)s"


def setup_logging() -> None:
    """Configure application-wide logging. Call once at startup."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # Clear existing handlers to avoid duplicates on reload
    root.handlers.clear()

    # Choose format - use custom formatter that injects request_id
    fmt = JSON_FORMAT if LOG_FORMAT == "json" else TEXT_FORMAT
    formatter = RequestIdFormatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    # Console handler (always enabled)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # File handler (optional)
    if LOG_FILE:
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("langchain").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the given name."""
    return logging.getLogger(name)
