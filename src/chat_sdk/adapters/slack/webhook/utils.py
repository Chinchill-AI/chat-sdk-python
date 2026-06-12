"""Helpers for the Slack webhook primitives subpath.

Port of ``packages/adapter-slack/src/webhook/utils.ts`` (vercel/chat#538),
plus :func:`read_slack_request_body` — the Python stand-in for the Fetch
API's ``await request.text()`` used by the upstream verify helpers. Python
web frameworks expose request bodies through several duck-typed shapes, so
the extraction lives here as a shared primitive (the high-level adapter
uses the same implementation).
"""

from __future__ import annotations

import inspect
import json
import math
from collections.abc import Iterable
from typing import Any, cast

from chat_sdk.adapters.slack.webhook.types import (
    SlackHeaders,
    SlackRetry,
    SlackWebhookParseError,
)


def get_header(headers: SlackHeaders | None, name: str) -> str | None:
    """Read a header case-insensitively from a mapping or pair-iterable."""
    if headers is None:
        return None
    lower = name.lower()
    items: Iterable[tuple[Any, Any]]
    # Prefer ``.items()`` (dicts, framework multidicts, header objects) over
    # bare iteration — most header containers iterate over *keys*, not pairs.
    items_attr = getattr(headers, "items", None)
    if callable(items_attr):
        items = cast("Iterable[tuple[Any, Any]]", items_attr())
    elif isinstance(headers, Iterable) and not isinstance(headers, (str, bytes)):
        items = cast("Iterable[tuple[Any, Any]]", headers)
    else:
        return None
    for key, value in items:
        if str(key).lower() == lower:
            return _header_value(value)
    return None


def get_retry(headers: SlackHeaders | None) -> SlackRetry | None:
    """Parse Slack retry headers into a :class:`SlackRetry`, if present."""
    retry_num = get_header(headers, "x-slack-retry-num")
    if not retry_num:
        return None
    num = _finite_number(retry_num)
    if num is None:
        return None
    return SlackRetry(num=num, reason=get_header(headers, "x-slack-retry-reason"))


def is_form_body(body: str, content_type: str) -> bool:
    """Decide whether a body should be parsed as form-urlencoded."""
    if "application/x-www-form-urlencoded" in content_type:
        return True
    if "application/json" in content_type:
        return False
    trimmed = body.lstrip()
    return not trimmed.startswith("{") and "=" in body


def parse_json_body(body: str) -> Any:
    """Parse a JSON body, raising :class:`SlackWebhookParseError` on failure."""
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SlackWebhookParseError("Slack webhook body is invalid JSON") from exc


def is_record(value: Any) -> bool:
    """True when ``value`` is a JSON object (dict)."""
    return isinstance(value, dict)


def record_value(value: Any) -> dict[str, Any] | None:
    """Return ``value`` when it is a dict, else ``None``."""
    return value if isinstance(value, dict) else None


def string_value(value: Any) -> str:
    """Return ``value`` when it is a string, else ``""``."""
    return value if isinstance(value, str) else ""


def optional_string(value: Any) -> str | None:
    """Return ``value`` when it is a non-empty string, else ``None``."""
    text = string_value(value)
    return text if text else None


async def read_slack_request_body(request: Any) -> str:
    """Read the raw body from a duck-typed request object.

    Python stand-in for the Fetch API's ``await request.text()``. Supports:

    - ``request.text`` as an (async or sync) method or plain attribute
    - ``request.body`` as an (async or sync) method, awaitable, bytes, or str
    - falling back to ``str(request)``

    Bytes are decoded as UTF-8.
    """
    text_attr = getattr(request, "text", None)
    if text_attr is not None:
        if callable(text_attr):
            result = text_attr()
            text_attr = await result if inspect.isawaitable(result) else result
        return text_attr.decode("utf-8") if isinstance(text_attr, (bytes, bytearray)) else str(text_attr)
    raw = getattr(request, "body", None)
    if raw is not None:
        # Some frameworks expose ``body`` as an async method (e.g.
        # ``async def body(self)``) — call it, then await if the result is
        # awaitable. Covers both the coroutine-as-attribute case and the
        # async-method case.
        if callable(raw):
            raw = raw()
        if inspect.isawaitable(raw):
            raw = await raw
        return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    return str(request)


def _header_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        first = value[0] if value else None
        return first if isinstance(first, str) else None
    return None


def _finite_number(text: str) -> float | int | None:
    """Parse a header numeric the way JS ``Number()`` would, or ``None``."""
    stripped = text.strip()
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        num = float(stripped)
    except ValueError:
        return None
    return num if math.isfinite(num) else None
