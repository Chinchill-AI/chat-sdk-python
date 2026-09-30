"""Tests for the Messenger attachment download guard.

Ports upstream ``packages/adapter-messenger/src/fetch.test.ts``
(``describe("Messenger attachment fetch")``, vercel/chat 153bd964) onto
``MessengerAdapter._download_attachment`` -- the Python analogue of the TS
``download(url, transport)`` helper. The injected TS ``transport`` maps to a
fake aiohttp session installed on ``adapter._http_session``; "transport never
called" maps to "no ``session.get`` call recorded".

Python-specific additions cover what upstream tests in the shared downloader
(``adapter-shared/src/download.test.ts``) or leaves implicit: redirects
between allowlisted hosts, the redirect cap, the 25 MB body cap (declared and
streamed), the overall deadline, and URL-parser-differential inputs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from multidict import CIMultiDict

from chat_sdk.adapters.messenger import adapter as messenger_module
from chat_sdk.adapters.messenger.adapter import MessengerAdapter
from chat_sdk.adapters.messenger.types import MessengerAdapterConfig
from chat_sdk.logger import ConsoleLogger
from chat_sdk.shared.errors import NetworkError

UNTRUSTED = "Refusing to fetch an untrusted attachment URL"


# ---------------------------------------------------------------------------
# Fakes (shared with tests/test_messenger_webhook.py)
# ---------------------------------------------------------------------------


class FakeContent:
    """Stand-in for ``aiohttp.StreamReader`` exposing ``iter_chunked``."""

    def __init__(self, chunks: list[bytes], hang: asyncio.Event | None = None) -> None:
        self._chunks = chunks
        self._hang = hang
        self.chunks_read = 0

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        if self._hang is not None:
            await self._hang.wait()
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk


class FakeResponse:
    """Minimal ``aiohttp.ClientResponse`` shape used by the download path."""

    def __init__(
        self,
        body: bytes = b"",
        status: int = 200,
        headers: dict[str, str] | None = None,
        reason: str = "OK",
        chunks: list[bytes] | None = None,
        hang: asyncio.Event | None = None,
    ) -> None:
        self.status = status
        self.reason = reason
        self.headers = CIMultiDict(headers or {})
        self.content = FakeContent(chunks if chunks is not None else [body], hang)


class FakeSession:
    """Records ``get`` calls and replays queued responses (or raises errors).

    ``session.get`` in aiohttp is a sync call returning an async context
    manager, so the returned handle is a ``MagicMock`` whose ``__aenter__`` /
    ``__aexit__`` are ``AsyncMock``s -- which also lets tests assert every
    response was released.
    """

    closed = False

    def __init__(self, *responses: FakeResponse | BaseException) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.contexts: list[MagicMock] = []

    @property
    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]

    def get(self, url: str, **kwargs: Any) -> MagicMock:
        self.calls.append((url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        handle = MagicMock()
        handle.__aenter__ = AsyncMock(return_value=item)
        handle.__aexit__ = AsyncMock(return_value=False)
        self.contexts.append(handle)
        return handle


def make_adapter(session: FakeSession | None = None) -> MessengerAdapter:
    adapter = MessengerAdapter(
        MessengerAdapterConfig(
            app_secret="test-app-secret",
            page_access_token="test-page-token",
            verify_token="test-verify-token",
            user_name="test-bot",
            logger=ConsoleLogger("error"),
        )
    )
    if session is not None:
        adapter._http_session = session
    return adapter


def redirect(location: str, status: int = 302) -> FakeResponse:
    return FakeResponse(status=status, headers={"Location": location}, reason="Found")


# ---------------------------------------------------------------------------
# Upstream: describe("Messenger attachment fetch")
# ---------------------------------------------------------------------------


class TestMessengerAttachmentFetch:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "https://cdn.fbsbx.com/file",
            "https://lookaside.fbsbx.com/file",
            "https://scontent.xx.fbcdn.net/file",
            "https://SContent.XX.FBCDN.NET/file",
        ],
    )
    async def test_downloads_from_meta_cdn_url(self, url: str) -> None:
        session = FakeSession(FakeResponse(b"media"))
        adapter = make_adapter(session)

        assert await adapter._download_attachment(url) == b"media"

        assert session.urls == [url]
        _, kwargs = session.calls[0]
        # Redirects are handled manually so every hop is re-validated, and the
        # per-request aiohttp timeout carries the 30 s budget.
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"].total == 30
        session.contexts[0].__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/file",
            "https://fbcdn.net.attacker.example/file",
            "https://cdn.fbsbx.com./file",
            "http://cdn.fbsbx.com/file",
            "https://127.0.0.1/file",
            "https://2130706433/file",
        ],
    )
    async def test_rejects_untrusted_attachment_url(self, url: str) -> None:
        session = FakeSession(FakeResponse(b"media"))
        adapter = make_adapter(session)

        with pytest.raises(NetworkError, match=UNTRUSTED):
            await adapter._download_attachment(url)
        assert session.calls == []

    @pytest.mark.asyncio
    async def test_rejects_redirects_away_from_meta_cdn_hosts(self) -> None:
        session = FakeSession(redirect("https://example.com/file"))
        adapter = make_adapter(session)

        with pytest.raises(NetworkError, match=UNTRUSTED):
            await adapter._download_attachment("https://cdn.fbsbx.com/file")
        assert session.urls == ["https://cdn.fbsbx.com/file"]

    @pytest.mark.asyncio
    async def test_normalizes_transport_failures(self) -> None:
        offline = RuntimeError("offline")
        adapter = make_adapter(FakeSession(offline))

        with pytest.raises(NetworkError) as excinfo:
            await adapter._download_attachment("https://cdn.fbsbx.com/file")
        assert excinfo.value.adapter == "messenger"
        assert str(excinfo.value) == "Failed to download Messenger attachment"
        assert excinfo.value.original_error is offline

    @pytest.mark.asyncio
    async def test_rejects_unsuccessful_responses(self) -> None:
        adapter = make_adapter(FakeSession(FakeResponse(b"missing", status=404, reason="Not Found")))

        with pytest.raises(NetworkError) as excinfo:
            await adapter._download_attachment("https://cdn.fbsbx.com/file")
        assert excinfo.value.adapter == "messenger"
        assert str(excinfo.value) == "Failed to fetch file: 404 Not Found"

    @pytest.mark.asyncio
    async def test_uses_messenger_network_errors(self) -> None:
        # No session injected: the refusal happens before the adapter even
        # creates its aiohttp session (upstream: default transport untouched).
        adapter = make_adapter()

        with pytest.raises(NetworkError) as excinfo:
            await adapter._download_attachment("https://example.com/file")
        assert excinfo.value.adapter == "messenger"
        assert str(excinfo.value) == UNTRUSTED
        assert adapter._http_session is None


# ---------------------------------------------------------------------------
# Python-specific coverage
# ---------------------------------------------------------------------------


class TestMessengerAttachmentFetchRedirects:
    @pytest.mark.asyncio
    async def test_follows_redirects_between_allowlisted_hosts(self) -> None:
        """Mirrors shared ``download.test.ts`` "follows redirects between allowlisted hosts"."""
        session = FakeSession(
            redirect("/v/t1/img.jpg", status=301),
            redirect("https://scontent.xx.fbcdn.net/v/t1/img.jpg?oh=sig", status=307),
            FakeResponse(b"image-bytes"),
        )
        adapter = make_adapter(session)

        assert await adapter._download_attachment("https://lookaside.fbsbx.com/start") == b"image-bytes"
        # The relative Location resolves against the current hop's URL.
        assert session.urls == [
            "https://lookaside.fbsbx.com/start",
            "https://lookaside.fbsbx.com/v/t1/img.jpg",
            "https://scontent.xx.fbcdn.net/v/t1/img.jpg?oh=sig",
        ]
        for handle in session.contexts:
            handle.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_follows_five_redirects(self) -> None:
        hops = [redirect(f"https://cdn.fbsbx.com/hop{i}") for i in range(1, 6)]
        session = FakeSession(*hops, FakeResponse(b"ok"))
        adapter = make_adapter(session)

        assert await adapter._download_attachment("https://cdn.fbsbx.com/hop0") == b"ok"
        assert len(session.calls) == 6

    @pytest.mark.asyncio
    async def test_rejects_a_sixth_redirect(self) -> None:
        hops = [redirect(f"https://cdn.fbsbx.com/hop{i}") for i in range(1, 7)]
        session = FakeSession(*hops)
        adapter = make_adapter(session)

        with pytest.raises(NetworkError, match="Too many attachment redirects"):
            await adapter._download_attachment("https://cdn.fbsbx.com/hop0")
        assert len(session.calls) == 6

    @pytest.mark.asyncio
    async def test_redirect_without_location_raises_network_error(self) -> None:
        adapter = make_adapter(FakeSession(FakeResponse(status=302, reason="Found")))

        with pytest.raises(NetworkError, match="Attachment redirect has no location"):
            await adapter._download_attachment("https://cdn.fbsbx.com/file")

    @pytest.mark.asyncio
    async def test_malformed_redirect_location_raises_network_error(self) -> None:
        # ``urlsplit`` raises ValueError on an unterminated IPv6 bracket; it
        # must surface as the untrusted-URL NetworkError, not a ValueError.
        session = FakeSession(redirect("https://[::1/file"))
        adapter = make_adapter(session)

        with pytest.raises(NetworkError, match=UNTRUSTED):
            await adapter._download_attachment("https://cdn.fbsbx.com/file")
        assert len(session.calls) == 1


class TestMessengerAttachmentFetchLimits:
    @pytest.mark.asyncio
    async def test_rejects_declared_content_length_over_cap_before_reading(self) -> None:
        assert messenger_module._ATTACHMENT_LIMIT_BYTES == 25 * 1024 * 1024
        response = FakeResponse(
            b"never-read",
            headers={"Content-Length": str(25 * 1024 * 1024 + 1)},
        )
        session = FakeSession(response)
        adapter = make_adapter(session)

        with pytest.raises(NetworkError, match="Attachment exceeds the download limit"):
            await adapter._download_attachment("https://cdn.fbsbx.com/file")
        assert response.content.chunks_read == 0
        session.contexts[0].__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aborts_streamed_body_over_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(messenger_module, "_ATTACHMENT_LIMIT_BYTES", 10)
        response = FakeResponse(chunks=[b"aaaa", b"bbbb", b"cccc", b"dddd"])
        adapter = make_adapter(FakeSession(response))

        with pytest.raises(NetworkError, match="Attachment exceeds the download limit"):
            await adapter._download_attachment("https://cdn.fbsbx.com/file")
        # 4 + 4 + 4 = 12 > 10: aborted on the third chunk, the fourth is never read.
        assert response.content.chunks_read == 3

    @pytest.mark.asyncio
    async def test_accepts_streamed_body_exactly_at_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(messenger_module, "_ATTACHMENT_LIMIT_BYTES", 10)
        response = FakeResponse(chunks=[b"aaaaa", b"bbbbb"], headers={"Content-Length": "10"})
        adapter = make_adapter(FakeSession(response))

        assert await adapter._download_attachment("https://cdn.fbsbx.com/file") == b"aaaaabbbbb"

    @pytest.mark.asyncio
    async def test_overall_deadline_times_out_a_stalled_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert messenger_module._ATTACHMENT_TIMEOUT_S == 30.0
        monkeypatch.setattr(messenger_module, "_ATTACHMENT_TIMEOUT_S", 0.01)
        never = asyncio.Event()
        adapter = make_adapter(FakeSession(FakeResponse(hang=never)))

        with pytest.raises(NetworkError) as excinfo:
            # Outer bound so a regression that drops the deadline fails
            # instead of hanging the suite.
            await asyncio.wait_for(adapter._download_attachment("https://cdn.fbsbx.com/file"), timeout=5)
        assert excinfo.value.adapter == "messenger"
        assert str(excinfo.value) == "Timed out fetching the attachment"
        assert isinstance(excinfo.value.original_error, TimeoutError)


class TestMessengerAttachmentUrlParsing:
    """Stricter-than-upstream URL checks (see docs/UPSTREAM_SYNC.md)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            "",
            "not a url",
            "//cdn.fbsbx.com/file",
            "ftp://cdn.fbsbx.com/file",
            "https://user@cdn.fbsbx.com/file",
            "https://evil.example@cdn.fbsbx.com/file",
            "https://cdn.fbsbx.com:8443/file",
            "https://cdn.fbsbx.com:99999/file",
            "https://evil.example\\.fbsbx.com/file",
            "https://cdn.fbsbx\t.com/file",
            "https://cdn.fbsbx.com /file",
            "https://%63dn.fbsbx.com/file",
            "https://[::1]/file",
            "https://[::ffff:127.0.0.1]/file",
            "https://[cdn.fbsbx.com]/file",
            "https://cdn..fbsbx.com/file",
            "https://cdnfbsbx.com/file",
        ],
    )
    async def test_rejects_ambiguous_or_non_default_urls(self, url: str) -> None:
        session = FakeSession(FakeResponse(b"media"))
        adapter = make_adapter(session)

        with pytest.raises(NetworkError, match=UNTRUSTED):
            await adapter._download_attachment(url)
        assert session.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        ["https://cdn.fbsbx.com:443/file", "HTTPS://fbcdn.net/file", "https://fbsbx.com/file"],
    )
    async def test_accepts_default_port_uppercase_scheme_and_apex_host(self, url: str) -> None:
        session = FakeSession(FakeResponse(b"media"))
        adapter = make_adapter(session)

        assert await adapter._download_attachment(url) == b"media"
        assert session.urls == [url]
