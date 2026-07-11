"""Structured logging setup for Maestro."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from maestro.config import Settings, get_settings


class JsonFormatter(logging.Formatter):
    """Format log records as JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a log record as a single JSON line."""

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key in ("executionId", "workItemId", "controller", "correlationId"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        exc_info = record.exc_info
        if exc_info is not None and exc_info[0] is not None:
            payload["exception"] = self.formatException(exc_info)

        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def configure_logging(settings: Settings | None = None) -> None:
    """Configure root logging with structured JSON output."""

    resolved_settings = settings or get_settings()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(resolved_settings.log_level.upper())
