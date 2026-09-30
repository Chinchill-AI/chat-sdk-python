"""Slack guarded file downloads and outbound egress plumbing (#213).

Ports the file-download cases of upstream
``packages/adapter-slack/src/index.test.ts`` and the ``describe("outbound
transports")`` cases of ``transport.test.ts`` (chat@4.41.1; vercel/chat
``7c269653`` #859, ``b6fa24c6`` #865, ``6adca361`` #916). The TS
``fileTransport`` spy maps to :class:`FakeFileTransport`, which records each
hop's URL and resolved headers; the TS ``fetch`` config maps to
``http_client_factory``. The Socket Mode transport case lives in
``tests/test_slack_socket_mode.py`` next to its SocketModeClient fake.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.slack.adapter import SlackAdapter, create_slack_adapter
from chat_sdk.adapters.slack.types import SlackAdapterConfig
from chat_sdk.shared.download import AttachmentTransport
from chat_sdk.shared.errors import NetworkError, ValidationError
from chat_sdk.types import Attachment
from tests._slack_file_transport import FakeFileResponse, FakeFileTransport

# Upstream ``fileMessageEvent`` (test-fixtures.ts).
FILE_MESSAGE_EVENT: dict[str, Any] = {
    "type": "message",
    "user": "U123",
    "channel": "C123",
    "text": "file",
    "ts": "1.1",
    "files": [
        {
            "id": "F123",
            "name": "file.pdf",
            "mimetype": "application/pdf",
            "url_private": "https://files.slack.com/file.pdf",
        }
    ],
}


def _adapter(**overrides: Any) -> SlackAdapter:
    return create_slack_adapter(
        SlackAdapterConfig(
            signing_secret=overrides.pop("signing_secret", "secret"),
            bot_token=overrides.pop("bot_token", "xoxb-test"),
            **overrides,
        )
    )


def _file_attachment(adapter: SlackAdapter, event: dict[str, Any] | None = None) -> Attachment:
    message = adapter.parse_message(event if event is not None else FILE_MESSAGE_EVENT)
    return message.attachments[0]


def _ephemeral_id(url: str = "https://hooks.slack.com/respond") -> str:
    data = json.dumps({"responseUrl": url, "userId": "U123"})
    return f"ephemeral:1.1:{base64.b64encode(data.encode()).decode()}"


class TestSlackFileDownloads:
    async def test_refuses_external_message_files_without_resolving_the_bot_token(self) -> None:
        # Adapted port of upstream "downloads external message files without
        # resolving the bot token": upstream fetches the external URL without
        # credentials; Python refuses URLs off the Slack allowlist
        # (docs/UPSTREAM_SYNC.md). Either way the token is never resolved.
        token = AsyncMock(return_value="xoxb-test")
        transport = FakeFileTransport()
        adapter = _adapter(bot_token=token, file_transport=transport)
        attachment = _file_attachment(
            adapter,
            {
                "type": "message",
                "user": "U123",
                "channel": "C456",
                "text": "External file",
                "ts": "1234567890.123456",
                "files": [
                    {
                        "id": "F123",
                        "mimetype": "application/vnd.slack-remote",
                        "url_private": "https://docs.google.com/document/d/external",
                    }
                ],
            },
        )

        assert attachment.fetch_data is not None
        with pytest.raises(ValidationError, match="untrusted URL"):
            await attachment.fetch_data()

        token.assert_not_called()
        assert transport.calls == []

    @pytest.mark.parametrize(
        "url",
        [
            # Parsed differently by ``urlsplit`` and the downloader.
            "https://attacker.example\\@files.slack.com/file.pdf",
            "http://files.slack.com/file.pdf",
            "https://127.0.0.1/file.pdf",
        ],
    )
    async def test_allowlist_applies_to_the_url_the_downloader_requests(self, url: str) -> None:
        token = MagicMock(return_value="xoxb-test")
        transport = FakeFileTransport()
        adapter = _adapter(bot_token=token, file_transport=transport)
        rehydrated = adapter.rehydrate_attachment(Attachment(type="file", url=url))
        assert rehydrated.fetch_data is not None

        with pytest.raises(ValidationError, match="untrusted URL"):
            await rehydrated.fetch_data()
        token.assert_not_called()
        assert transport.calls == []

    async def test_uses_the_configured_guarded_transport_for_lazy_and_rehydrated_files(self) -> None:
        transport = FakeFileTransport(FakeFileResponse(b"file"))
        adapter = _adapter(file_transport=transport)
        attachment = _file_attachment(adapter)
        assert transport.calls == []

        assert attachment.fetch_data is not None
        assert await attachment.fetch_data() == b"file"
        rehydrated = adapter.rehydrate_attachment(Attachment(type="file", url="https://files.slack.com/file.pdf"))
        assert rehydrated.fetch_data is not None
        assert await rehydrated.fetch_data() == b"file"

        assert [url for url, _ in transport.calls] == ["https://files.slack.com/file.pdf"] * 2
        assert transport.authorizations == ["Bearer xoxb-test"] * 2

    async def test_preserves_subclass_overrides_over_the_configured_transport(self) -> None:
        configured = FakeFileTransport()
        subclass_transport = FakeFileTransport(FakeFileResponse(b"file"))

        class CustomAdapter(SlackAdapter):
            def _create_file_transport(self) -> AttachmentTransport | None:
                return subclass_transport

        adapter = CustomAdapter(
            SlackAdapterConfig(signing_secret="secret", bot_token="xoxb-test", file_transport=configured)
        )
        attachment = _file_attachment(adapter)
        assert attachment.fetch_data is not None
        await attachment.fetch_data()

        assert len(subclass_transport.calls) == 1
        assert configured.calls == []

    async def test_delegates_public_host_redirects_to_the_transport_without_credentials_and_rejects_internal_literals(
        self,
    ) -> None:
        transport = FakeFileTransport(
            FakeFileResponse(b"", status=302, headers={"location": "https://cdn.example/file"}),
            FakeFileResponse(b"file"),
        )
        adapter = _adapter(file_transport=transport)
        attachment = _file_attachment(adapter)
        assert attachment.fetch_data is not None
        assert await attachment.fetch_data() == b"file"

        assert transport.authorizations[0] == "Bearer xoxb-test"
        assert transport.calls[1][0] == "https://cdn.example/file"
        assert transport.authorizations[1] is None

        internal = FakeFileTransport(FakeFileResponse(b"", status=302, headers={"location": "https://127.0.0.1/file"}))
        adapter._file_transport = internal
        with pytest.raises(NetworkError, match="internal attachment URL"):
            await attachment.fetch_data()
        assert len(internal.calls) == 1

    async def test_redirect_back_to_a_slack_auth_origin_carries_the_token_again(self) -> None:
        # Headers are resolved per hop: only hops on a Slack auth origin get
        # the token, including one reached after an off-origin hop.
        transport = FakeFileTransport(
            FakeFileResponse(b"", status=302, headers={"location": "https://edge.slack.com/x"}),
            FakeFileResponse(b"", status=302, headers={"location": "https://files.slack.com/final.pdf"}),
            FakeFileResponse(b"file"),
        )
        attachment = _file_attachment(_adapter(file_transport=transport))
        assert attachment.fetch_data is not None
        await attachment.fetch_data()

        assert transport.authorizations == ["Bearer xoxb-test", None, "Bearer xoxb-test"]

    async def test_non_auth_allowlisted_url_is_fetched_without_resolving_the_token(self) -> None:
        # ``*.slack-edge.com`` is on the Python allowlist, but it is not a
        # Slack auth origin, so the token is neither resolved nor sent.
        token = MagicMock(return_value="xoxb-test")
        transport = FakeFileTransport(FakeFileResponse(b"avatar"))
        adapter = _adapter(bot_token=token, file_transport=transport)
        rehydrated = adapter.rehydrate_attachment(Attachment(type="image", url="https://ca.slack-edge.com/T-U-x-512"))
        assert rehydrated.fetch_data is not None

        assert await rehydrated.fetch_data() == b"avatar"
        token.assert_not_called()
        assert transport.authorizations == [None]

    async def test_authenticates_files_on_the_configured_api_origin(self) -> None:
        transport = FakeFileTransport(FakeFileResponse(b"file"))
        adapter = _adapter(api_url="https://slack-proxy.example/api/", file_transport=transport)
        rehydrated = adapter.rehydrate_attachment(
            Attachment(type="file", url="https://slack-proxy.example/files-pri/T/F/report.txt")
        )
        assert rehydrated.fetch_data is not None
        await rehydrated.fetch_data()

        assert transport.authorizations == ["Bearer xoxb-test"]

    @pytest.mark.parametrize(
        ("response", "message"),
        [
            (
                FakeFileResponse(b"login", headers={"Content-Type": "text/html; charset=utf-8"}),
                "received HTML login page",
            ),
            (FakeFileResponse(b"denied", status=403, reason="Forbidden"), "Failed to fetch file: 403 Forbidden"),
            (
                FakeFileResponse(gzip.compress(bytes(26 * 1024 * 1024)), headers={"content-encoding": "gzip"}),
                "Attachment exceeds the download limit",
            ),
        ],
        ids=["HTML login", "non-success", "decoded size"],
    )
    async def test_keeps_rejection_with_a_configured_transport(self, response: FakeFileResponse, message: str) -> None:
        attachment = _file_attachment(_adapter(file_transport=FakeFileTransport(response)))
        assert attachment.fetch_data is not None
        with pytest.raises(NetworkError, match=message) as info:
            await attachment.fetch_data()
        assert info.value.adapter == "slack"
        assert response.closed

    async def test_wraps_transport_failures_as_a_slack_network_error(self) -> None:
        cause = OSError("connection reset")

        async def failing(url: str, headers: dict[str, str]) -> Any:
            raise cause

        attachment = _file_attachment(_adapter(file_transport=failing))
        assert attachment.fetch_data is not None
        with pytest.raises(NetworkError, match="^Failed to fetch Slack file$") as info:
            await attachment.fetch_data()
        assert info.value.original_error is cause

    @pytest.mark.parametrize(("elapsed_s", "times_out"), [(29.9, False), (30.0, True)])
    async def test_enforces_the_30_second_download_deadline(
        self, monkeypatch: pytest.MonkeyPatch, elapsed_s: float, times_out: bool
    ) -> None:
        # Python counterpart of upstream "enforces the download deadline when
        # a signal-ignoring transport %s" (which shortens AbortSignal.timeout
        # and asserts the 30_000 ms default): the transport moves the loop
        # clock forward, so the default deadline is checked without sleeping.
        loop = asyncio.get_running_loop()
        real_time = loop.time
        offset = [0.0]
        monkeypatch.setattr(loop, "time", lambda: real_time() + offset[0])
        response = FakeFileResponse(b"file")

        async def slow(url: str, headers: dict[str, str]) -> Any:
            offset[0] += elapsed_s
            return response

        attachment = _file_attachment(_adapter(file_transport=slow))
        assert attachment.fetch_data is not None
        if times_out:
            with pytest.raises(NetworkError, match="Timed out fetching the attachment"):
                await attachment.fetch_data()
        else:
            assert await attachment.fetch_data() == b"file"

    async def test_enforces_the_30_second_download_deadline_when_the_body_stalls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The "stalls the body" case of upstream "enforces the download
        # deadline when a signal-ignoring transport %s": the response arrives
        # at once but its body never yields, and the loop clock is moved past
        # the 30 s default while the body read is pending.
        loop = asyncio.get_running_loop()
        real_time = loop.time
        offset = [0.0]
        monkeypatch.setattr(loop, "time", lambda: real_time() + offset[0])
        stalled = FakeFileResponse(hang=True)
        attachment = _file_attachment(_adapter(file_transport=FakeFileTransport(stalled)))
        assert attachment.fetch_data is not None
        fetch = asyncio.ensure_future(attachment.fetch_data())
        await asyncio.wait_for(stalled.reading.wait(), timeout=5)
        offset[0] += 30.0
        with pytest.raises(NetworkError, match="Timed out fetching the attachment"):
            await asyncio.wait_for(fetch, timeout=5)
        assert stalled.closed


class _RecordingHttpResponse:
    def __init__(self, status: int = 200, text: str = "ok") -> None:
        self.status_code = status
        self.is_success = 200 <= status < 300
        self.text = text


class _RecordingHttpClient:
    """Stand-in ``httpx.AsyncClient`` returned by ``http_client_factory``."""

    def __init__(self, posts: list[dict[str, Any]]) -> None:
        self._posts = posts
        self.closed = False

    async def __aenter__(self) -> _RecordingHttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def post(self, url: str, *, json: Any, headers: dict[str, str]) -> _RecordingHttpResponse:
        self._posts.append({"url": url, "json": json, "headers": headers})
        return _RecordingHttpResponse()


class TestSlackResponseUrlClient:
    @pytest.mark.parametrize("factory", [create_slack_adapter, SlackAdapter], ids=["factory", "constructor"])
    async def test_uses_configured_http_client_for_response_replacements_and_deletions(
        self, monkeypatch: pytest.MonkeyPatch, factory: Any
    ) -> None:
        # Port of upstream "uses configured fetch for response replacements
        # and deletions"; ``fetch`` maps to ``http_client_factory``.
        import httpx

        def unexpected(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("unexpected default httpx client")

        monkeypatch.setattr(httpx, "AsyncClient", unexpected)
        posts: list[dict[str, Any]] = []
        clients: list[_RecordingHttpClient] = []

        def make_client() -> Any:
            clients.append(_RecordingHttpClient(posts))
            return clients[-1]

        adapter = factory(
            SlackAdapterConfig(signing_secret="secret", bot_token="xoxb-test", http_client_factory=make_client)
        )
        await adapter.edit_message("slack:C123:1.1", _ephemeral_id(), "updated")
        await adapter.delete_message("slack:C123:1.1", _ephemeral_id())

        assert len(posts) == 2
        assert posts[0]["url"] == "https://hooks.slack.com/respond"
        assert posts[0]["json"]["replace_original"] is True
        assert posts[0]["json"]["text"] == "updated"
        assert posts[1]["json"] == {"delete_original": True}
        # One fresh client per request, closed by the adapter.
        assert len(clients) == 2
        assert all(client.closed for client in clients)

        with pytest.raises(ValidationError):
            await adapter.delete_message("slack:C123:1.1", _ephemeral_id("https://evil.example"))
        assert len(posts) == 2
        assert len(clients) == 2
