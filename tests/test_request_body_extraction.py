"""Regression tests for `_get_request_body` body-extraction across adapters.

Each adapter's webhook handler normalizes the incoming request object via a
framework-agnostic `_get_request_body` helper. The helper supports multiple
frameworks by duck-typing on `request.text` and `request.body` — each of which
may be:

- a plain string/bytes attribute (Django raw HttpRequest, some mocks)
- a synchronous method (older frameworks, test helpers)
- an async method / coroutine-returning callable (aiohttp, FastAPI, Starlette)

Historical bugs in this code path have been:

1. `hasattr(request, "text") and callable(request.text)` gate silently dropped
   non-callable `request.text` string attributes, falling through to `body`.
   Fixed across 5 adapters by restructuring the branch.
2. `await request.text()` without `isawaitable` narrowing crashed on Flask-style
   sync `text` methods with `TypeError: object is not awaitable`. Fixed by
   guarding with `inspect.isawaitable`.
3. `request.body` exposed as an async method (`async def body(self)`) returned
   a coroutine that was stringified instead of awaited. Fixed by adding the
   same callable + isawaitable dance for `body`.
4. `isinstance(body, bytes)` missed `bytearray`. Fixed by using
   `(bytes, bytearray)` consistently.

Each test below locks in one of those cases per adapter.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fake request objects covering the shapes we need to support.
# ---------------------------------------------------------------------------


class _StringTextRequest:
    """`request.text` is a populated string attribute (Django-style)."""

    def __init__(self, body: str) -> None:
        self.text = body


class _BytesTextRequest:
    """`request.text` is populated bytes."""

    def __init__(self, body: bytes) -> None:
        self.text = body


class _BytearrayTextRequest:
    """`request.text` is a bytearray."""

    def __init__(self, body: bytearray) -> None:
        self.text = body


class _SyncCallableTextRequest:
    """`request.text` is a sync method (Flask-style)."""

    def __init__(self, body: str) -> None:
        self._body = body

    def text(self) -> str:  # pragma: no cover - called
        return self._body


class _AsyncCallableTextRequest:
    """`request.text` is an async method (aiohttp/FastAPI)."""

    def __init__(self, body: str) -> None:
        self._body = body

    async def text(self) -> str:
        return self._body


class _AsyncCallableBodyRequest:
    """`request.body` is an async method returning bytes."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _SyncCallableBodyRequest:
    """`request.body` is a sync method returning bytes."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def body(self) -> bytes:  # pragma: no cover - called
        return self._body


class _PropertyBodyRequest:
    """`request.body` is a bytes property."""

    def __init__(self, body: bytes) -> None:
        self.body = body


class _BytearrayBodyRequest:
    """`request.body` is a bytearray."""

    def __init__(self, body: bytearray) -> None:
        self.body = body


# ---------------------------------------------------------------------------
# Per-adapter matrix — each adapter exposes `_get_request_body` (static or
# instance). We run the same 7 cases through each to lock in the contract.
# ---------------------------------------------------------------------------


def _adapters() -> list[tuple[str, Any]]:
    """Return (name, extractor) pairs for each adapter's body extraction.

    Lazy imports because some adapters require platform deps (cryptography,
    pynacl). If a dep isn't installed, that adapter is skipped.
    """
    result: list[tuple[str, Any]] = []

    # GitHub (static method)
    try:
        from chat_sdk.adapters.github.adapter import GitHubAdapter

        result.append(("github", GitHubAdapter._get_request_body))
    except ImportError:
        pass  # Optional platform dep missing — skip this adapter's cases.

    # Telegram (static method)
    try:
        from chat_sdk.adapters.telegram.adapter import TelegramAdapter

        result.append(("telegram", TelegramAdapter._get_request_body))
    except ImportError:
        pass  # Optional platform dep missing — skip this adapter's cases.

    # WhatsApp (static method)
    try:
        from chat_sdk.adapters.whatsapp.adapter import WhatsAppAdapter

        result.append(("whatsapp", WhatsAppAdapter._get_request_body))
    except ImportError:
        pass  # Optional platform dep missing — skip this adapter's cases.

    return result


@pytest.mark.parametrize("name,extractor", _adapters())
async def test_handles_string_text_attribute(name: str, extractor: Any) -> None:
    """Non-callable `request.text` string attribute is consumed — the
    historical bug was silently falling through to `body`, which
    produced `<Req>`-style stringified objects for valid webhooks.
    """
    result = await extractor(_StringTextRequest('{"ok": true}'))
    assert result == '{"ok": true}', f"{name} failed string text attr"


@pytest.mark.parametrize("name,extractor", _adapters())
async def test_handles_bytes_text_attribute(name: str, extractor: Any) -> None:
    """Bytes `request.text` is decoded as UTF-8."""
    result = await extractor(_BytesTextRequest(b'{"ok": true}'))
    assert result == '{"ok": true}', f"{name} failed bytes text attr"


@pytest.mark.parametrize("name,extractor", _adapters())
async def test_handles_bytearray_text_attribute(name: str, extractor: Any) -> None:
    """Bytearray `request.text` is also decoded — bytes/bytearray symmetry."""
    result = await extractor(_BytearrayTextRequest(bytearray(b'{"ok": true}')))
    assert result == '{"ok": true}', f"{name} failed bytearray text attr"


@pytest.mark.parametrize("name,extractor", _adapters())
async def test_handles_async_callable_text(name: str, extractor: Any) -> None:
    """`async def text(self)` returning a coroutine is awaited."""
    result = await extractor(_AsyncCallableTextRequest('{"ok": true}'))
    assert result == '{"ok": true}', f"{name} failed async text()"


@pytest.mark.parametrize("name,extractor", _adapters())
async def test_handles_sync_callable_text(name: str, extractor: Any) -> None:
    """Sync `def text(self)` returns the body directly — the
    historical bug was `await request.text()` crashing on this path.
    """
    result = await extractor(_SyncCallableTextRequest('{"ok": true}'))
    assert result == '{"ok": true}', f"{name} failed sync text()"


@pytest.mark.parametrize("name,extractor", _adapters())
async def test_handles_async_callable_body(name: str, extractor: Any) -> None:
    """Falls back to `request.body` when no `text`. Historical bug:
    `async def body(self)` returning a coroutine was stringified as
    `<coroutine object ...>` instead of awaited, breaking webhook
    signature verification on FastAPI/Starlette.
    """
    result = await extractor(_AsyncCallableBodyRequest(b'{"ok": true}'))
    assert result == '{"ok": true}', f"{name} failed async body()"


@pytest.mark.parametrize("name,extractor", _adapters())
async def test_handles_bytes_body_attribute(name: str, extractor: Any) -> None:
    """`request.body` as a bytes property is decoded as UTF-8."""
    result = await extractor(_PropertyBodyRequest(b'{"ok": true}'))
    assert result == '{"ok": true}', f"{name} failed bytes body attr"


@pytest.mark.parametrize("name,extractor", _adapters())
async def test_handles_bytearray_body_attribute(name: str, extractor: Any) -> None:
    """`request.body` as bytearray — bytes/bytearray symmetry on the body
    path, catching the asymmetry where we fixed text-path for bytearray
    but left body-path checking only `isinstance(body, bytes)`.
    """
    result = await extractor(_BytearrayBodyRequest(bytearray(b'{"ok": true}')))
    assert result == '{"ok": true}', f"{name} failed bytearray body attr"
