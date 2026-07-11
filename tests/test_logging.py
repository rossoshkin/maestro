"""Tests for structured logging."""

import json
import logging

from maestro.logging import JsonFormatter


def test_json_formatter_includes_standard_context() -> None:
    record = logging.LogRecord(
        name="maestro.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.executionId = "execution-1"
    record.correlationId = "correlation-1"

    formatted = JsonFormatter().format(record)

    payload = json.loads(formatted)
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["executionId"] == "execution-1"
    assert payload["correlationId"] == "correlation-1"
