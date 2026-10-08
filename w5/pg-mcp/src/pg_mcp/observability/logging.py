"""Structured logging configuration for PostgreSQL MCP Server.

This module provides JSON-formatted structured logging with automatic
sanitization of sensitive data (passwords, API keys, PII).
"""

import json
import logging
import sys
from typing import Any, ClassVar

from pydantic import BaseModel


class LogRecord(BaseModel):
    """Structured log record model.

    Attributes:
        timestamp: ISO 8601 timestamp.
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        logger: Logger name.
        message: Log message.
        request_id: Optional request ID for tracing.
        **extra: Additional context fields.
    """

    timestamp: str
    level: str
    logger: str
    message: str
    request_id: str | None = None
    extra: dict[str, Any] | None = None


class SensitiveDataFilter(logging.Filter):
    """Filter to sanitize sensitive data from log records.

    This filter removes or masks sensitive information such as:
    - Database passwords
    - API keys
    - Tokens
    - Personal Identifiable Information (PII)

    Example:
        >>> handler = logging.StreamHandler()
        >>> handler.addFilter(SensitiveDataFilter())
    """

    SENSITIVE_KEYS: ClassVar[set[str]] = {
        "password",
        "passwd",
        "pwd",
        "secret",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "private_key",
        "client_secret",
        "auth",
        "authorization",
    }

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter and sanitize the log record.

        Args:
            record: The log record to filter.

        Returns:
            bool: Always True to allow the record through (after sanitization).
        """
        # Sanitize args if they exist
        if hasattr(record, "args") and record.args:
            record.args = self._sanitize_data(record.args)

        # Sanitize extra fields
        if hasattr(record, "__dict__"):
            for key in list(record.__dict__.keys()):
                if key.lower() in self.SENSITIVE_KEYS:
                    record.__dict__[key] = "***REDACTED***"
                elif isinstance(record.__dict__[key], dict):
                    record.__dict__[key] = self._sanitize_dict(record.__dict__[key])

        return True

    def _sanitize_data(self, data: Any) -> Any:
        """Recursively sanitize data structures.

        Args:
            data: Data to sanitize (dict, list, tuple, or primitive).

        Returns:
            Sanitized copy of the data.
        """
        if isinstance(data, dict):
            return self._sanitize_dict(data)
        elif isinstance(data, (list, tuple)):
            return type(data)(self._sanitize_data(item) for item in data)
        return data

    def _sanitize_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        """Sanitize dictionary keys that may contain sensitive data.

        Args:
            data: Dictionary to sanitize.

        Returns:
            Sanitized dictionary.
        """
        sanitized: dict[str, Any] = {}
        for key, value in data.items():
            if key.lower() in self.SENSITIVE_KEYS:
                sanitized[key] = "***REDACTED***"
            elif isinstance(value, dict):
                sanitized[key] = self._sanitize_dict(value)
            elif isinstance(value, (list, tuple)):
                sanitized_items = [self._sanitize_data(item) for item in value]
                sanitized[key] = type(value)(sanitized_items)
            else:
                sanitized[key] = value
        return sanitized


class RequestContextFilter(logging.Filter):
    """Attach the ambient tracing context to every log record.

    ``request_id`` and ``operation`` live in :mod:`contextvars` (see
    :mod:`pg_mcp.observability.tracing`). This filter copies them onto each
    record so that *every* log line emitted while handling a request carries
    the correlation ids, without callers having to pass ``extra`` manually.

    Example:
        >>> handler = logging.StreamHandler()
        >>> handler.addFilter(RequestContextFilter())
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Copy request_id/operation from context onto the record.

        Args:
            record: The log record to enrich.

        Returns:
            bool: Always True - the record is never dropped.
        """
        # Imported lazily: tracing imports nothing from this module, but keeping
        # the import local makes the dependency direction obvious and avoids any
        # chance of an import cycle through the package __init__.
        from pg_mcp.observability.tracing import get_operation, get_request_id

        if getattr(record, "request_id", None) is None:
            request_id = get_request_id()
            if request_id is not None:
                record.request_id = request_id

        if getattr(record, "operation", None) is None:
            operation = get_operation()
            if operation is not None:
                record.operation = operation

        return True


class JSONFormatter(logging.Formatter):
    """JSON log formatter for structured logging.

    Formats log records as JSON objects with consistent structure,
    making them suitable for log aggregation systems.

    Example:
        >>> handler = logging.StreamHandler()
        >>> handler.setFormatter(JSONFormatter())
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as JSON.

        Args:
            record: The log record to format.

        Returns:
            JSON-formatted log string.
        """
        log_data = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add request_id if present
        if hasattr(record, "request_id"):
            log_data["request_id"] = record.request_id

        # Add operation if present
        if hasattr(record, "operation"):
            log_data["operation"] = record.operation

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Add extra fields
        extra_fields = {}
        for key, value in record.__dict__.items():
            if key not in [
                "name",
                "msg",
                "args",
                "created",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "thread",
                "threadName",
                "exc_info",
                "exc_text",
                "stack_info",
                "request_id",
                "operation",
            ]:
                extra_fields[key] = value

        if extra_fields:
            log_data["extra"] = extra_fields

        return json.dumps(log_data, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable text formatter for development.

    Formats logs in a readable format suitable for console output
    during development.

    Example:
        >>> handler = logging.StreamHandler()
        >>> handler.setFormatter(TextFormatter())
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as readable text.

        Args:
            record: The log record to format.

        Returns:
            Formatted log string.
        """
        # Base format: timestamp [level] logger - message
        formatted = (
            f"{self.formatTime(record, self.datefmt)} "
            f"[{record.levelname}] "
            f"{record.name} - "
            f"{record.getMessage()}"
        )

        # Add request_id if present
        if hasattr(record, "request_id"):
            formatted += f" [request_id={record.request_id}]"

        # Add operation if present
        if hasattr(record, "operation"):
            formatted += f" [operation={record.operation}]"

        # Add exception if present
        if record.exc_info:
            formatted += f"\n{self.formatException(record.exc_info)}"

        return formatted


def configure_logging(
    level: str = "INFO",
    log_format: str = "json",
    enable_sensitive_filter: bool = True,
) -> None:
    """Configure application logging with structured output.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_format: Format type ("json" or "text").
        enable_sensitive_filter: Whether to enable sensitive data filtering.

    Example:
        >>> configure_logging(level="DEBUG", log_format="json")
        >>> logger = logging.getLogger(__name__)
        >>> logger.info("Processing query", extra={"request_id": "123"})
    """
    # Remove existing handlers
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create console handler. Logs MUST go to stderr: the MCP stdio transport
    # uses stdout exclusively for JSON-RPC frames, so anything else written
    # there corrupts the protocol stream.
    handler = logging.StreamHandler(sys.stderr)

    # Set formatter
    formatter: logging.Formatter
    if log_format == "json":
        formatter = JSONFormatter(datefmt="%Y-%m-%dT%H:%M:%S")
    else:
        formatter = TextFormatter(datefmt="%Y-%m-%d %H:%M:%S")

    handler.setFormatter(formatter)

    # Add sensitive data filter
    if enable_sensitive_filter:
        handler.addFilter(SensitiveDataFilter())

    # Propagate tracing context (request_id/operation) into every record
    handler.addFilter(RequestContextFilter())

    # Configure root logger
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, level.upper()))

    # Reduce noise from third-party libraries
    logging.getLogger("asyncpg").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # SQLGlot warns (at WARNING) whenever it falls back to a generic Command
    # node - for example for EXPLAIN - which is part of our normal validation
    # flow, so keep it out of the application logs.
    logging.getLogger("sqlglot").setLevel(logging.ERROR)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the given name.

    Args:
        name: Logger name (typically __name__).

    Returns:
        Configured logger instance.

    Example:
        >>> logger = get_logger(__name__)
        >>> logger.info("Operation completed")
    """
    return logging.getLogger(name)
