"""Tests for inbound Teams attachment retrieval (``teams/attachments.py``).

Ported from ``packages/adapter-teams/src/attachments.test.ts`` (chat@4.41.1,
describe "Teams attachments"), plus Python-specific cases for the bot-token
fetch helper ``fetch_with_bot_token``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from unittest.mock import AsyncMock

import httpx
import pytest

from chat_sdk.adapters.teams.attachments import (
    TeamsAttachmentFetchers,
    create_anonymous_attachment_fetch_data,
    create_teams_attachment,
    fetch_with_bot_token,
    rehydrate_teams_attachment,
)
from chat_sdk.shared.errors import NetworkError
from chat_sdk.types import Attachment

CONNECTOR_URL = "https://smba.trafficmanager.net/teams/"
FILE_DOWNLOAD_INFO = "application/vnd.microsoft.teams.file.download.info"


def _fetchers(
    fetch_authenticated: AsyncMock,
    transfer: AsyncMock | None = None,
) -> TeamsAttachmentFetchers:
    """Upstream ``createFetchers``: a ``transfer`` spy replaces the anonymous downloader."""
    create: Callable[[str], Callable[[], object]]
    if transfer is not None:

        def create(url: str):
            async def fetch() -> bytes:
                return await transfer(url)

            return fetch

    else:
        create = create_anonymous_attachment_fetch_data
    return TeamsAttachmentFetchers(
        create_anonymous_fetch_data=create,  # type: ignore[arg-type]
        fetch_authenticated=fetch_authenticated,
    )


def _roundtrip(attachment: Attachment) -> Attachment:
    """JSON round trip of the serializable fields (upstream ``JSON.parse(JSON.stringify(...))``)."""
    data = json.loads(
        json.dumps(
            {
                "type": attachment.type,
                "url": attachment.url,
                "mime_type": attachment.mime_type,
                "fetch_metadata": attachment.fetch_metadata,
            }
        )
    )
    return Attachment(**data)


class TestTeamsAttachments:
    @pytest.mark.asyncio
    async def test_downloads_file_cards_anonymously_and_infers_their_mime_type(self):
        fetch_authenticated = AsyncMock()
        transfer = AsyncMock(return_value=b"file contents")
        url = "https://contoso-my.sharepoint.com/personal/user/file.png"

        attachment = create_teams_attachment(
            {
                "contentType": FILE_DOWNLOAD_INFO,
                "content": {"downloadUrl": url, "fileType": ".png"},
                "name": "diagram.png",
            },
            CONNECTOR_URL,
            _fetchers(fetch_authenticated, transfer),
        )

        assert attachment.type == "image"
        assert attachment.url == url
        assert attachment.name == "diagram.png"
        assert attachment.mime_type == "image/png"
        assert attachment.fetch_metadata == {"url": url}
        assert attachment.fetch_data is not None
        assert await attachment.fetch_data() == b"file contents"
        transfer.assert_awaited_once_with(url)
        fetch_authenticated.assert_not_awaited()

    @pytest.mark.parametrize(
        ("file_type", "mime_type"),
        [
            (".pdf", "application/pdf"),
            (".xls", "application/vnd.ms-excel"),
            (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ],
    )
    def test_infers_the_mime_type_for_file_cards(self, file_type: str, mime_type: str):
        attachment = create_teams_attachment(
            {
                "contentType": FILE_DOWNLOAD_INFO,
                "content": {"downloadUrl": "https://files.example.com/download", "fileType": file_type},
                "name": f"report{file_type}",
            },
            CONNECTOR_URL,
            _fetchers(AsyncMock()),
        )

        assert attachment.type == "file"
        assert attachment.mime_type == mime_type

    def test_infers_file_card_mime_type_from_the_name_without_file_type(self):
        """Upstream falls back to the name's extension (``name?.split(".").pop()``);
        an unknown or missing extension is ``application/octet-stream``."""
        named = create_teams_attachment(
            {
                "contentType": FILE_DOWNLOAD_INFO,
                "content": {"downloadUrl": "https://x.sharepoint.com/d"},
                "name": "A.JPG",
            },
            CONNECTOR_URL,
            _fetchers(AsyncMock()),
        )
        unknown = create_teams_attachment(
            {
                "contentType": FILE_DOWNLOAD_INFO,
                "content": {"downloadUrl": "https://x.sharepoint.com/d"},
                "name": "notes",
            },
            CONNECTOR_URL,
            _fetchers(AsyncMock()),
        )
        assert named.mime_type == "image/jpeg"
        assert named.type == "image"
        assert unknown.mime_type == "application/octet-stream"

    def test_file_card_without_download_url_has_no_fetch(self):
        """A file card's ``contentUrl`` (the SharePoint item, 403 anonymously) is never used."""
        attachment = create_teams_attachment(
            {
                "contentType": FILE_DOWNLOAD_INFO,
                "contentUrl": "https://contoso.sharepoint.com/personal/u/Documents/x.pdf",
                "name": "x.pdf",
                "content": {"fileType": "pdf"},
            },
            CONNECTOR_URL,
            _fetchers(AsyncMock()),
        )
        assert attachment.url is None
        assert attachment.fetch_metadata is None
        assert attachment.fetch_data is None

    @pytest.mark.asyncio
    async def test_keeps_cross_origin_inline_attachments_anonymous(self):
        fetch_authenticated = AsyncMock()
        transfer = AsyncMock(return_value=b"public image")
        url = "https://files.example.com/image.png"

        attachment = create_teams_attachment(
            {"contentType": "image/png", "contentUrl": url, "name": "image.png"},
            CONNECTOR_URL,
            _fetchers(fetch_authenticated, transfer),
        )

        assert attachment.type == "image"
        assert attachment.url == url
        assert attachment.name == "image.png"
        assert attachment.mime_type == "image/png"
        assert attachment.fetch_metadata == {"url": url}
        assert attachment.fetch_data is not None
        assert await attachment.fetch_data() == b"public image"
        transfer.assert_awaited_once_with(url)
        fetch_authenticated.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_plain_http_inline_attachment_downloads_by_default(self):
        fetch_authenticated = AsyncMock()
        url = "http://contoso-my.sharepoint.com/image.png"

        attachment = create_teams_attachment(
            {"contentType": "image/png", "contentUrl": url, "name": "image.png"},
            CONNECTOR_URL,
            _fetchers(fetch_authenticated),
        )

        assert attachment.fetch_data is not None
        with pytest.raises(NetworkError, match="Refusing to fetch an untrusted attachment URL"):
            await attachment.fetch_data()
        fetch_authenticated.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_authenticates_emulator_attachments_on_a_loopback_http_connector(self):
        fetch_authenticated = AsyncMock(return_value=b"emulator image")
        url = "http://localhost:3978/v3/attachments/image/views/original"

        attachment = create_teams_attachment(
            {"contentType": "image/png", "contentUrl": url, "name": "image.png"},
            "http://localhost:3978/",
            _fetchers(fetch_authenticated),
        )

        assert attachment.fetch_metadata == {
            "url": url,
            "auth": "bot",
            "connectorOrigin": "http://localhost:3978",
        }
        assert attachment.fetch_data is not None
        assert await attachment.fetch_data() == b"emulator image"
        fetch_authenticated.assert_awaited_once_with(url)

    @pytest.mark.parametrize(
        ("url", "service_url", "origin"),
        [
            # Scheme/host compare lowercased, the default port is dropped.
            (
                "https://SMBA.TrafficManager.net:443/teams/v3/attachments/a",
                CONNECTOR_URL,
                "https://smba.trafficmanager.net",
            ),
            # A non-default port is part of the origin.
            ("http://127.0.0.1:3978/v3/attachments/a", "http://127.0.0.1:3978", "http://127.0.0.1:3978"),
            ("http://[::1]:3978/v3/attachments/a", "http://[::1]:3978/", "http://[::1]:3978"),
        ],
    )
    def test_connector_origin_normalization(self, url: str, service_url: str, origin: str):
        attachment = create_teams_attachment(
            {"contentType": "image/png", "contentUrl": url},
            service_url,
            _fetchers(AsyncMock()),
        )
        assert attachment.fetch_metadata == {"url": url, "auth": "bot", "connectorOrigin": origin}

    @pytest.mark.parametrize(
        ("url", "service_url"),
        [
            # A different port is a different origin.
            ("https://smba.trafficmanager.net:8443/teams/a", CONNECTOR_URL),
            # Plain http off loopback never gets the token, even on the connector host.
            ("http://smba.trafficmanager.net/teams/a", "http://smba.trafficmanager.net/teams/"),
            # Userinfo and backslashes are where URL parsers disagree about the host.
            ("https://evil.example\\@smba.trafficmanager.net/teams/a", CONNECTOR_URL),
            ("https://user@smba.trafficmanager.net/teams/a", CONNECTOR_URL),
        ],
    )
    def test_ambiguous_or_cross_origin_urls_stay_anonymous(self, url: str, service_url: str):
        attachment = create_teams_attachment(
            {"contentType": "image/png", "contentUrl": url},
            service_url,
            _fetchers(AsyncMock()),
        )
        assert attachment.fetch_metadata == {"url": url}

    @pytest.mark.asyncio
    async def test_rehydrates_both_retrieval_modes_and_revalidates_bot_destinations(self):
        fetch_authenticated = AsyncMock(return_value=b"rehydrated image")
        transfer = AsyncMock(return_value=b"anonymous file")
        fetchers = _fetchers(fetch_authenticated, transfer)
        url = "https://smba.trafficmanager.net/teams/v3/attachments/image/views/original"
        serialized = _roundtrip(
            Attachment(
                type="image",
                url=url,
                fetch_metadata={"url": url, "auth": "bot", "connectorOrigin": "https://smba.trafficmanager.net"},
            )
        )

        rehydrated = rehydrate_teams_attachment(serialized, fetchers)
        assert rehydrated.fetch_data is not None
        assert await rehydrated.fetch_data() == b"rehydrated image"

        for untrusted_url, connector_origin in [
            ("https://files.example.com/image.png", "https://smba.trafficmanager.net"),
            ("http://smba.trafficmanager.net/teams/image.png", "https://smba.trafficmanager.net"),
            (url, None),
        ]:
            meta = {"url": untrusted_url, "auth": "bot"}
            if connector_origin:
                meta["connectorOrigin"] = connector_origin
            attachment = rehydrate_teams_attachment(
                Attachment(type="image", url=untrusted_url, fetch_metadata=meta),
                fetchers,
            )
            assert attachment.fetch_data is not None
            with pytest.raises(NetworkError, match="Refusing to send a bot token to an untrusted attachment URL"):
                await attachment.fetch_data()

        anonymous_url = "https://files.example.com/report.pdf"
        anonymous = rehydrate_teams_attachment(
            _roundtrip(
                Attachment(
                    type="file",
                    url=anonymous_url,
                    mime_type="application/pdf",
                    fetch_metadata={"url": anonymous_url},
                )
            ),
            fetchers,
        )
        assert anonymous.fetch_data is not None
        assert await anonymous.fetch_data() == b"anonymous file"

        assert fetch_authenticated.await_count == 1
        transfer.assert_awaited_once_with(anonymous_url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data",
            "https://127.0.0.1/private",
            "https://2130706433/private",
            "https://[::1]/private",
        ],
    )
    @pytest.mark.asyncio
    async def test_rejects_internal_anonymous_url_after_rehydration(self, url: str):
        attachment = rehydrate_teams_attachment(
            Attachment(type="file", url=url, fetch_metadata={"url": url}),
            _fetchers(AsyncMock()),
        )

        assert attachment.fetch_data is not None
        with pytest.raises(NetworkError, match="Refusing to fetch an internal attachment URL"):
            await attachment.fetch_data()

    def test_rehydrate_keeps_the_other_fields(self):
        """Rehydration only rebuilds ``fetch_data`` (upstream ``{...attachment, fetchData}``)."""
        original = Attachment(
            type="image",
            url="https://x.sharepoint.com/a.png",
            name="a.png",
            mime_type="image/png",
            size=12,
            width=3,
            height=4,
            fetch_metadata={"url": "https://x.sharepoint.com/a.png"},
        )
        rehydrated = rehydrate_teams_attachment(original, _fetchers(AsyncMock()))
        assert rehydrated.fetch_data is not None
        assert (rehydrated.name, rehydrated.mime_type, rehydrated.size, rehydrated.width, rehydrated.height) == (
            "a.png",
            "image/png",
            12,
            3,
            4,
        )
        assert rehydrated.fetch_metadata == original.fetch_metadata


# ---------------------------------------------------------------------------
# fetch_with_bot_token (Python-specific transport for the authenticated path)
# ---------------------------------------------------------------------------

CONNECTOR_ATTACHMENT = "https://smba.trafficmanager.net/teams/v3/attachments/a/views/original"


async def _streamed(data: bytes):
    """A streamed body (``httpx.Response(content=bytes)`` would be pre-read)."""
    yield data


class _SdkClient:
    """Stand-in for the Teams SDK HTTP client: injects the bot token, owns an ``httpx`` client."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _prepare_headers(self, headers: dict[str, str] | None, token: object) -> dict[str, str]:
        assert token is None
        return {**(headers or {}), "Authorization": "Bearer bot-token"}


class TestFetchWithBotToken:
    @pytest.mark.asyncio
    async def test_sends_the_sdk_token_and_returns_the_body(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=_streamed(b"protected image"))

        assert await fetch_with_bot_token(_SdkClient(handler), CONNECTOR_ATTACHMENT) == b"protected image"
        assert [str(r.url) for r in seen] == [CONNECTOR_ATTACHMENT]
        assert seen[0].headers["authorization"] == "Bearer bot-token"

    @pytest.mark.asyncio
    async def test_a_connector_redirect_errors_and_is_not_followed(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://evil.example/collect"})

        with pytest.raises(NetworkError, match="Failed to fetch authenticated file: 302"):
            await fetch_with_bot_token(_SdkClient(handler), CONNECTOR_ATTACHMENT)
        assert seen == [CONNECTOR_ATTACHMENT]

    @pytest.mark.asyncio
    async def test_rejects_a_declared_length_over_the_limit(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-length": "11"}, content=_streamed(b"x" * 11))

        with pytest.raises(NetworkError, match="Attachment exceeds the download limit"):
            await fetch_with_bot_token(_SdkClient(handler), CONNECTOR_ATTACHMENT, limit=10)

    @pytest.mark.asyncio
    async def test_rejects_a_streamed_body_over_the_limit(self):
        async def chunks():
            for _ in range(4):
                yield b"x" * 4

        def handler(request: httpx.Request) -> httpx.Response:
            # No Content-Length: the cap must apply to the streamed bytes.
            return httpx.Response(200, content=chunks())

        with pytest.raises(NetworkError, match="Attachment exceeds the download limit"):
            await fetch_with_bot_token(_SdkClient(handler), CONNECTOR_ATTACHMENT, limit=10)

    @pytest.mark.asyncio
    async def test_non_2xx_raises_with_the_status(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        with pytest.raises(NetworkError, match="Failed to fetch authenticated file: 403"):
            await fetch_with_bot_token(_SdkClient(handler), CONNECTOR_ATTACHMENT)
