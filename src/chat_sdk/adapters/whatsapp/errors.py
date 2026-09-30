"""Typed errors for the WhatsApp adapter.

Python port of packages/adapter-whatsapp/src/errors.ts.
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from chat_sdk.adapters.whatsapp.types import WhatsAppGraphError
from chat_sdk.shared.errors import AdapterError

# Longest slice of a response body kept in the error message.
_MESSAGE_BODY_LIMIT = 500
# ASCII-only and used with ``fullmatch`` so it behaves like JS
# ``/^-?\d+$/`` (Python's ``\d`` matches any Unicode digit and ``$``
# matches before a trailing newline).
_INTEGER_STRING = re.compile(r"-?[0-9]+")

# Meta error codes that the Graph API uses for throttling.
# See: https://developers.facebook.com/documentation/business-messaging/whatsapp/support/error-codes/
_RATE_LIMIT_CODES = frozenset({4, 17, 32, 613, 80_007, 130_429, 131_048, 131_056})
_AUTH_CODES = frozenset({0, 190})
_PERMISSION_CODES = frozenset({3, 10})


def _record(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _integer(value: Any) -> int | None:
    """Read an integer field, accepting the numeric strings that proxies and
    emulators in front of the Cloud API sometimes emit.

    ``bool`` is rejected explicitly because it subclasses ``int`` in Python
    (JS ``typeof true`` is ``"boolean"``). An integral float such as ``4.0``
    is accepted, matching JS where ``JSON.parse("4.0")`` is the number ``4``
    and ``Number.isInteger(4)`` is true.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str) and _INTEGER_STRING.fullmatch(value):
        return int(value)
    return None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _parse_graph_error(raw: Any) -> WhatsAppGraphError | None:
    root = _record(raw)
    error = _record(root.get("error")) if root is not None else None
    if error is None:
        return None
    error_data = _record(error.get("error_data"))
    details = _string(error_data.get("details")) if error_data is not None else None
    parsed: WhatsAppGraphError = {}
    code = _integer(error.get("code"))
    if code is not None:
        parsed["code"] = code
    if details is not None:
        parsed["error_data"] = {"details": details}
    subcode = _integer(error.get("error_subcode"))
    if subcode is not None:
        parsed["error_subcode"] = subcode
    trace_id = _string(error.get("fbtrace_id"))
    if trace_id is not None:
        parsed["fbtrace_id"] = trace_id
    message = _string(error.get("message"))
    if message is not None:
        parsed["message"] = message
    error_type = _string(error.get("type"))
    if error_type is not None:
        parsed["type"] = error_type
    return parsed


def _taxonomy_code(status: int, code: int | None = None) -> str | None:
    """Map an HTTP status and Meta error code onto the shared
    ``AdapterError.code`` taxonomy so cross-adapter handlers can branch
    without WhatsApp knowledge."""
    if status == 429 or (code is not None and code in _RATE_LIMIT_CODES):
        return "RATE_LIMITED"
    if status == 401 or (code is not None and code in _AUTH_CODES):
        return "AUTH_FAILED"
    if status == 403 or (code is not None and (code in _PERMISSION_CODES or 200 <= code <= 299)):
        return "PERMISSION_DENIED"
    if status == 404:
        return "NOT_FOUND"
    return None


def _reject_constant(name: str) -> Any:
    # JS ``JSON.parse`` rejects ``NaN``/``Infinity``; Python accepts them by
    # default. Reject so such bodies stay text in ``raw`` as upstream.
    raise ValueError(f"invalid JSON constant: {name}")


class WhatsAppApiError(AdapterError):
    """A non-2xx response from the Meta Graph API.

    ``code`` follows the shared ``AdapterError`` taxonomy (``RATE_LIMITED``,
    ``AUTH_FAILED``, ``PERMISSION_DENIED``, ``NOT_FOUND``) when the status or
    Meta error code maps onto it. Meta's own numeric code is exposed as
    ``error_code``.

    Attributes use snake_case (``error_code``, ``provider_message``,
    ``trace_id``) where upstream uses camelCase (``errorCode``,
    ``providerMessage``, ``traceId``).
    """

    status: int
    """HTTP response status."""
    error_code: int | None
    """Meta's numeric error code, such as ``130429``."""
    provider_message: str | None
    """Meta's human-readable ``error.message``."""
    type: str | None
    """Meta's ``error.type``, such as ``"OAuthException"``."""
    details: str | None
    """Meta's ``error.error_data.details``."""
    subcode: int | None
    """Meta's ``error.error_subcode``. Optional and deprecated in the Cloud API."""
    trace_id: str | None
    """Meta's ``error.fbtrace_id``."""
    raw: Any
    """The parsed response body, shaped like ``WhatsAppGraphErrorBody`` when
    Meta answered with its error envelope, or the original text when the body
    is not valid JSON."""

    def __init__(self, message: str, status: int, body: str) -> None:
        raw: Any = body
        # Keep the text body for non-JSON responses such as proxy error pages.
        with contextlib.suppress(ValueError):
            raw = json.loads(body, parse_constant=_reject_constant)

        error = _parse_graph_error(raw)
        provider_message = error.get("message") if error is not None else None
        if provider_message is not None:
            summary = provider_message
        elif len(body) > _MESSAGE_BODY_LIMIT:
            summary = f"{body[:_MESSAGE_BODY_LIMIT]}…"
        else:
            summary = body
        error_code = error.get("code") if error is not None else None
        super().__init__(f"{message}: {status} {summary}", "whatsapp", _taxonomy_code(status, error_code))
        self.status = status
        self.raw = raw
        self.error_code = error_code
        self.provider_message = provider_message
        self.type = error.get("type") if error is not None else None
        error_data = error.get("error_data") if error is not None else None
        self.details = error_data.get("details") if error_data is not None else None
        self.subcode = error.get("error_subcode") if error is not None else None
        self.trace_id = error.get("fbtrace_id") if error is not None else None
