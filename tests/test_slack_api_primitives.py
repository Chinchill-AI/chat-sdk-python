"""Tests for the runtime-free Slack Web API primitives subpath.

Port of ``packages/adapter-slack/src/api/index.test.ts`` and
``api/boundary.test.ts`` (vercel/chat#548, #559), exposed upstream as
``@chat-adapter/slack/api``. These primitives never touch the network in
tests: a fake ``fetch`` (an :class:`~unittest.mock.AsyncMock`) is injected
and its recorded calls are asserted, mirroring upstream's ``vi.fn()``
request mocks.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import pytest

from chat_sdk.adapters.slack.api import (
    SlackApiError,
    call_slack_api,
    delete_slack_message,
    encode_slack_api_body,
    fetch_slack_file,
    fetch_slack_thread_replies,
    open_slack_view,
    post_slack_ephemeral,
    post_slack_message,
    send_slack_response_url,
    update_slack_message,
    upload_slack_files,
)
from chat_sdk.adapters.slack.api import (
    SlackFileUpload as FileUpload,
)


class _JsonResponse:
    """Minimal stand-in for the injected fetch's response object.

    Exposes the ``status`` int and a sync ``json()`` returning the parsed
    body — the shape :func:`call_slack_api` reads.
    """

    def __init__(self, value: Any, status: int = 200) -> None:
        self._value = value
        self.status = status

    def json(self) -> Any:
        return self._value


def _json_response(value: Any, status: int = 200) -> _JsonResponse:
    return _JsonResponse(value, status)


def _form(call_args: Any) -> dict[str, list[str]]:
    """Parse the form-urlencoded body of a recorded fetch call."""
    body = call_args.kwargs["body"]
    return parse_qs(str(body), keep_blank_values=True)


def _url(call_args: Any) -> str:
    return call_args.args[0]


class TestSlackApiPrimitives:
    def test_form_encodes_slack_api_bodies_with_json_object_values(self) -> None:
        encoded = encode_slack_api_body(
            {
                "blocks": [{"type": "section"}],
                "channel": "C123",
                "reply_broadcast": False,
                "text": "hello",
                "thread_ts": None,
            }
        )

        assert encoded.content_type == "application/x-www-form-urlencoded"
        params = parse_qs(encoded.body)
        assert params["blocks"] == ['[{"type":"section"}]']
        assert params["reply_broadcast"] == ["false"]
        # None (TS undefined) is omitted entirely.
        assert "thread_ts" not in params

    async def test_calls_slack_web_api_with_bearer_token_auth(self) -> None:
        async def token() -> str:
            return "xoxb-token"

        request = AsyncMock(return_value=_json_response({"ok": True}))

        await call_slack_api(
            "chat.postMessage",
            {"channel": "C123", "text": "hello"},
            token=token,
            fetch=request,
        )

        call = request.await_args
        assert _url(call) == "https://slack.com/api/chat.postMessage"
        assert call.kwargs["headers"]["authorization"] == "Bearer xoxb-token"
        assert _form(call)["text"] == ["hello"]

    async def test_supports_custom_api_origins_for_tests_and_proxies(self) -> None:
        request = AsyncMock(return_value=_json_response({"ok": True}))

        await call_slack_api(
            "chat.postMessage",
            {},
            api_url="https://proxy.example/slack/",
            token="xoxb-token",
            fetch=request,
        )

        assert _url(request.await_args) == "https://proxy.example/slack/chat.postMessage"

    async def test_throws_for_non_2xx_slack_api_http_responses(self) -> None:
        request = AsyncMock(return_value=_json_response({"error": "ratelimited", "ok": False}, status=429))

        with pytest.raises(SlackApiError) as exc_info:
            await call_slack_api("chat.postMessage", {}, token="xoxb", fetch=request)

        assert exc_info.value.method == "chat.postMessage"
        assert exc_info.value.status == 429

    async def test_posts_messages_and_returns_the_slack_timestamp(self) -> None:
        request = AsyncMock(return_value=_json_response({"channel": "C123", "ok": True, "ts": "1.23"}))

        result = await post_slack_message(
            channel="C123",
            markdown_text="**hello**",
            token="xoxb",
            unfurl_links=False,
            unfurl_media=False,
            fetch=request,
        )

        params = _form(request.await_args)
        assert params["markdown_text"] == ["**hello**"]
        assert "text" not in params
        assert "blocks" not in params
        assert params["unfurl_links"] == ["false"]
        assert result.channel == "C123"
        assert result.id == "1.23"
        assert result.raw == {"channel": "C123", "ok": True, "ts": "1.23"}

    async def test_rejects_markdown_text_conflicts_locally(self) -> None:
        request = AsyncMock()

        with pytest.raises(TypeError):
            await post_slack_message(
                channel="C123",
                markdown_text="**hello**",
                text="hello",
                token="xoxb",
                fetch=request,
            )

        # The conflict is caught before any request is made.
        request.assert_not_awaited()

    async def test_posts_ephemeral_messages(self) -> None:
        request = AsyncMock(return_value=_json_response({"channel": "C123", "message_ts": "1.24", "ok": True}))

        result = await post_slack_ephemeral(
            channel="C123",
            text="hello",
            token="xoxb",
            user="U123",
            fetch=request,
        )

        call = request.await_args
        assert _url(call) == "https://slack.com/api/chat.postEphemeral"
        assert _form(call)["user"] == ["U123"]
        assert result.id == "1.24"

    async def test_updates_messages(self) -> None:
        request = AsyncMock(return_value=_json_response({"channel": "C123", "ok": True, "ts": "1.25"}))

        result = await update_slack_message(
            blocks=[{"type": "section"}],
            channel="C123",
            text="fallback",
            token="xoxb",
            ts="1.23",
            fetch=request,
        )

        call = request.await_args
        assert _url(call) == "https://slack.com/api/chat.update"
        params = _form(call)
        assert params["ts"] == ["1.23"]
        assert params["blocks"] == ['[{"type":"section"}]']
        assert result.id == "1.25"

    async def test_deletes_messages(self) -> None:
        request = AsyncMock(return_value=_json_response({"ok": True, "ts": "1.23"}))

        await delete_slack_message(
            channel="C123",
            token="xoxb",
            ts="1.23",
            fetch=request,
        )

        call = request.await_args
        assert _url(call) == "https://slack.com/api/chat.delete"
        params = _form(call)
        assert params["channel"] == ["C123"]
        assert params["ts"] == ["1.23"]

    async def test_throws_slack_api_error_for_ok_false_helper_responses(self) -> None:
        request = AsyncMock(return_value=_json_response({"error": "channel_not_found", "ok": False}))

        with pytest.raises(SlackApiError):
            await post_slack_message(
                channel="C123",
                text="hello",
                token="xoxb",
                fetch=request,
            )

    async def test_sends_response_url_json_payloads(self) -> None:
        request = AsyncMock(return_value=_json_response(None, status=200))

        await send_slack_response_url(
            "https://hooks.slack.com/actions/T/1/abc",
            replace_original=True,
            text="updated",
            fetch=request,
        )

        call = request.await_args
        assert _url(call) == "https://hooks.slack.com/actions/T/1/abc"
        assert json.loads(call.kwargs["body"]) == {
            "replace_original": True,
            "text": "updated",
        }

    async def test_rejects_non_slack_response_urls(self) -> None:
        """Python-specific SSRF guard: response_url must be https://*.slack.com.

        Diverges from upstream, which POSTs to any response_url. See
        ``docs/UPSTREAM_SYNC.md`` Known Non-Parity.
        """
        request = AsyncMock()

        with pytest.raises(ValueError, match="https://\\*.slack.com"):
            await send_slack_response_url(
                "https://evil.example/steal",
                text="x",
                fetch=request,
            )

        request.assert_not_awaited()

    async def test_uploads_files_with_slack_external_upload_flow(self) -> None:
        request = AsyncMock(
            side_effect=[
                _json_response(
                    {
                        "file_id": "F123",
                        "ok": True,
                        "upload_url": "https://files.slack.com/upload/v1/abc",
                    }
                ),
                _json_response(None, status=200),
                _json_response({"files": [{"id": "F123"}], "ok": True}),
            ]
        )

        result = await upload_slack_files(
            [FileUpload(data=bytes([1, 2, 3]), filename="report.txt")],
            channel_id="C123",
            initial_comment="here",
            thread_ts="1.23",
            token="xoxb",
            fetch=request,
        )

        calls = request.await_args_list
        assert _url(calls[0]) == "https://slack.com/api/files.getUploadURLExternal"
        assert _form(calls[0])["length"] == ["3"]
        assert _url(calls[1]) == "https://files.slack.com/upload/v1/abc"
        assert calls[1].kwargs["headers"]["authorization"] == "Bearer xoxb"
        assert _url(calls[2]) == "https://slack.com/api/files.completeUploadExternal"
        assert result.file_ids == ["F123"]

    async def test_fetches_private_slack_file_urls_with_bearer_auth(self) -> None:
        response = _json_response("file", status=200)
        request = AsyncMock(return_value=response)

        result = await fetch_slack_file(
            token="xoxb",
            url="https://files.slack.com/files-pri/T/F/report.txt",
            fetch=request,
        )

        assert result is response
        assert request.await_args.kwargs["headers"]["authorization"] == "Bearer xoxb"

    async def test_refuses_to_fetch_files_from_untrusted_hosts(self) -> None:
        """Python-specific token-leak guard: file host must be Slack-owned.

        Diverges from upstream, which forwards the bearer token to any URL.
        See ``docs/UPSTREAM_SYNC.md`` Known Non-Parity.
        """
        request = AsyncMock()

        with pytest.raises(ValueError, match="untrusted URL"):
            await fetch_slack_file(
                token="xoxb",
                url="https://evil.example/files-pri/x",
                fetch=request,
            )

        request.assert_not_awaited()

    async def test_fetches_thread_replies_with_cursor_metadata(self) -> None:
        request = AsyncMock(
            return_value=_json_response(
                {
                    "messages": [{"text": "root", "ts": "1.23"}],
                    "ok": True,
                    "response_metadata": {"next_cursor": "next"},
                }
            )
        )

        result = await fetch_slack_thread_replies(
            channel="C123",
            limit=50,
            token="xoxb",
            ts="1.23",
            fetch=request,
        )

        call = request.await_args
        assert _url(call) == "https://slack.com/api/conversations.replies"
        assert _form(call)["ts"] == ["1.23"]
        assert result.messages == [{"text": "root", "ts": "1.23"}]
        assert result.next_cursor == "next"
        assert result.raw == {
            "messages": [{"text": "root", "ts": "1.23"}],
            "ok": True,
            "response_metadata": {"next_cursor": "next"},
        }

    async def test_omits_next_cursor_when_metadata_absent(self) -> None:
        """An empty or missing ``next_cursor`` resolves to ``None``."""
        request = AsyncMock(
            return_value=_json_response({"messages": [], "ok": True, "response_metadata": {"next_cursor": ""}})
        )

        result = await fetch_slack_thread_replies(
            channel="C123",
            token="xoxb",
            ts="1.23",
            fetch=request,
        )

        assert result.next_cursor is None
        assert result.messages == []

    async def test_opens_slack_views_with_trigger_ids(self) -> None:
        request = AsyncMock(return_value=_json_response({"ok": True, "view": {"id": "V123", "type": "modal"}}))

        result = await open_slack_view(
            token="xoxb",
            trigger_id="trigger",
            view={"type": "modal"},
            fetch=request,
        )

        call = request.await_args
        assert _url(call) == "https://slack.com/api/views.open"
        assert json.loads(_form(call)["view"][0]) == {"type": "modal"}
        assert result.view == {"id": "V123", "type": "modal"}

    async def test_open_view_requires_trigger_or_interactivity_pointer(self) -> None:
        request = AsyncMock()

        with pytest.raises(TypeError, match="trigger_id or interactivity_pointer"):
            await open_slack_view(token="xoxb", view={"type": "modal"}, fetch=request)

        request.assert_not_awaited()


class TestApiImportBoundary:
    def test_does_not_import_the_full_adapter_or_runtime_packages(self) -> None:
        """Importing the api subpath must not pull in slack_sdk, an HTTP
        client, or the high-level adapter module (port of upstream's
        ``api/boundary.test.ts``)."""
        code = (
            "import sys\n"
            "import chat_sdk.adapters.slack.api\n"
            "forbidden = [\n"
            "    'slack_sdk',\n"
            "    'httpx',\n"
            "    'aiohttp',\n"
            "    'chat_sdk.adapters.slack.adapter',\n"
            "]\n"
            "loaded = [name for name in forbidden if name in sys.modules]\n"
            "assert not loaded, f'api subpath imported runtime modules: {loaded}'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
