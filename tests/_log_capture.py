"""Shared helpers for log-hygiene assertions (issue #187).

Mirrors upstream's ``stringifyLoggerCalls`` / ``findLoggedSentinels`` helpers
from ``adapter-github/src/index.test.ts`` (fc7df9c4): stringify every recorded
logger call, then assert that no payload sentinel, no raw body, and no retired
log key made it into the output.

Works with both ``chat_sdk.testing.MockLogger`` (per-level ``_CallRecorder``
with ``.calls``) and ``MagicMock`` loggers (``mock_calls`` — which also covers
calls made through ``logger.child(...)``).
"""

from __future__ import annotations

import json
from typing import Any

# Token-shaped values and a customer slug, as in upstream's
# ``WEBHOOK_LOG_SENTINELS``. Payloads under test embed these; none may appear
# in any logger call.
WEBHOOK_LOG_SENTINELS: tuple[str, ...] = (
    "secret-access-token",
    "secret-refresh-token",
    "Bearer secret-token",
    "Authorization",
    "customer-team-slug",
    "access_token",
    "refresh_token",
)

# Multi-byte text (2-, 3- and 4-byte UTF-8 sequences) so ``bodyBytes`` /
# ``bodyLength`` assertions distinguish byte length from code-point length.
MULTIBYTE_TEXT = "café ☃ 🚀"

_LEVELS = ("debug", "info", "warn", "error")


def _recorded_calls(logger: Any) -> list[Any]:
    mock_calls = getattr(logger, "mock_calls", None)
    if mock_calls is not None:
        # MagicMock logger: every call on it or its children, with args + kwargs.
        return [[str(c[0]), list(c[1]), dict(c[2])] for c in mock_calls]
    # MockLogger: per-level recorders.
    return [[level, list(args)] for level in _LEVELS for args in getattr(logger, level).calls]


def stringify_logger_calls(logger: Any) -> str:
    """Serialize every recorded logger call (message + context) to one string."""
    return json.dumps(_recorded_calls(logger), default=repr, ensure_ascii=False)


def logged_contexts(recorder: Any, message: str) -> list[Any]:
    """Contexts of every call to one ``MockLogger`` level with this message.

    A call logged without a context yields ``None``.
    """
    return [c[1] if len(c) > 1 else None for c in recorder.calls if c and c[0] == message]


def find_logged_sentinels(logger: Any, sentinels: tuple[str, ...] = WEBHOOK_LOG_SENTINELS) -> list[str]:
    """Return the sentinels that appear anywhere in the recorded logger calls."""
    logged = stringify_logger_calls(logger)
    return [s for s in sentinels if s in logged]


def assert_body_not_logged(logger: Any, body: str) -> None:
    """Assert neither the raw body nor any sentinel reached the logger.

    Checks the body both verbatim and in its JSON-escaped form: a JSON body
    stringified inside the serialized calls has its quotes escaped, so a bare
    substring check alone would pass vacuously.
    """
    logged = stringify_logger_calls(logger)
    assert find_logged_sentinels(logger) == []
    assert body not in logged
    assert json.dumps(body, ensure_ascii=False)[1:-1] not in logged
    assert "bodyPreview" not in logged
    assert "raw body" not in logged
