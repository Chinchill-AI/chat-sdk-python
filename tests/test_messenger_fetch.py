"""Tests for the Messenger attachment download guard.

Ports upstream ``packages/adapter-messenger/src/fetch.test.ts``
(``describe("Messenger attachment fetch")``, vercel/chat 153bd964 / b6fa24c6)
onto ``MessengerAdapter._download_attachment`` -- the Python analogue of the
TS ``download(url, transport)`` helper, which now delegates to the shared
guarded downloader (#239). The injected TS ``transport`` is the same
``transport`` argument here (an in-memory fake that records each hop).

Redirect limits, the body cap, the deadline and URL canonicalization are the
shared downloader's and are tested in ``tests/test_shared_download.py``.
"""

from __future__ import annotations

import pytest

from chat_sdk.adapters.messenger.adapter import MessengerAdapter
from chat_sdk.adapters.messenger.types import MessengerAdapterConfig
from chat_sdk.logger import ConsoleLogger
from chat_sdk.shared import download as download_module
from chat_sdk.shared.errors import NetworkError
from tests._slack_file_transport import FakeFileResponse, FakeFileTransport

UNTRUSTED = "Refusing to fetch an untrusted attachment URL"


def make_adapter() -> MessengerAdapter:
    return MessengerAdapter(
        MessengerAdapterConfig(
            app_secret="test-app-secret",
            page_access_token="test-page-token",
            verify_token="test-verify-token",
            user_name="test-bot",
            logger=ConsoleLogger("error"),
        )
    )


def install_transport(monkeypatch: pytest.MonkeyPatch, *responses: FakeFileResponse) -> FakeFileTransport:
    """Make the downloader's default transport a recording fake.

    For paths that cannot pass ``transport=`` (``Attachment.fetch_data``
    closures); the DNS-pinned default transport is never built.
    """
    transport = FakeFileTransport(*responses)
    monkeypatch.setattr(download_module, "create_transport", lambda _adapter: transport)
    return transport


class FailingTransport:
    """A transport whose request fails (upstream: ``throw new Error("offline")``)."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def __call__(self, url: str, headers: dict[str, str]) -> FakeFileResponse:
        self.calls += 1
        raise self.error


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
        transport = FakeFileTransport(FakeFileResponse(b"media"))

        assert await make_adapter()._download_attachment(url, transport) == b"media"

        # The host is canonicalized (lowercased) before the request, and no
        # credentials are sent to the CDN.
        assert [hop for hop, _ in transport.calls] == [url.replace("SContent.XX.FBCDN.NET", "scontent.xx.fbcdn.net")]
        assert transport.authorizations == [None]

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
        transport = FakeFileTransport(FakeFileResponse(b"media"))

        with pytest.raises(NetworkError) as excinfo:
            await make_adapter()._download_attachment(url, transport)
        assert excinfo.value.adapter == "messenger"
        assert transport.calls == []

    @pytest.mark.asyncio
    async def test_rejects_redirects_away_from_meta_cdn_hosts(self) -> None:
        transport = FakeFileTransport(
            FakeFileResponse(b"", status=302, headers={"location": "https://example.com/file"}, reason="Found")
        )

        with pytest.raises(NetworkError, match=UNTRUSTED):
            await make_adapter()._download_attachment("https://cdn.fbsbx.com/file", transport)
        assert len(transport.calls) == 1

    @pytest.mark.asyncio
    async def test_normalizes_transport_failures(self) -> None:
        offline = RuntimeError("offline")

        with pytest.raises(NetworkError) as excinfo:
            await make_adapter()._download_attachment("https://cdn.fbsbx.com/file", FailingTransport(offline))
        assert excinfo.value.adapter == "messenger"
        assert str(excinfo.value) == "Failed to download Messenger attachment"
        assert excinfo.value.original_error is offline

    @pytest.mark.asyncio
    async def test_rejects_unsuccessful_responses(self) -> None:
        transport = FakeFileTransport(FakeFileResponse(b"missing", status=404, reason="Not Found"))

        with pytest.raises(NetworkError) as excinfo:
            await make_adapter()._download_attachment("https://cdn.fbsbx.com/file", transport)
        assert excinfo.value.adapter == "messenger"
        assert str(excinfo.value) == "Failed to fetch file: 404 Not Found"

    @pytest.mark.asyncio
    async def test_uses_messenger_network_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No transport passed: the default transport is refused before any
        # request (upstream calls ``download(url)`` without a transport).
        default_transport = install_transport(monkeypatch, FakeFileResponse(b"media"))

        with pytest.raises(NetworkError) as excinfo:
            await make_adapter()._download_attachment("https://example.com/file")
        assert excinfo.value.adapter == "messenger"
        assert str(excinfo.value) == UNTRUSTED
        assert default_transport.calls == []


# ---------------------------------------------------------------------------
# Python-specific coverage
# ---------------------------------------------------------------------------


class TestMessengerAttachmentFetchPythonSpecific:
    @pytest.mark.asyncio
    async def test_follows_redirects_between_meta_cdn_hosts_without_credentials(self) -> None:
        transport = FakeFileTransport(
            FakeFileResponse(b"", status=301, headers={"location": "/v/t1/img.jpg"}),
            FakeFileResponse(
                b"", status=307, headers={"location": "https://scontent.xx.fbcdn.net/v/t1/img.jpg?oh=sig"}
            ),
            FakeFileResponse(b"image-bytes"),
        )

        data = await make_adapter()._download_attachment("https://lookaside.fbsbx.com/start", transport)

        assert data == b"image-bytes"
        assert [hop for hop, _ in transport.calls] == [
            "https://lookaside.fbsbx.com/start",
            "https://lookaside.fbsbx.com/v/t1/img.jpg",
            "https://scontent.xx.fbcdn.net/v/t1/img.jpg?oh=sig",
        ]
        assert transport.authorizations == [None, None, None]
