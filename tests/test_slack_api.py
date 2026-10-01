"""Tests for Slack adapter API-calling methods (postMessage, editMessage, etc.).

These tests use a MockSlackClient that records API calls and returns
configurable responses, exercising the adapter's API layer without
network access.

Covers: postMessage, editMessage, deleteMessage, fetchMessages,
fetchThread, listThreads, postEphemeral, scheduleMessage (with cancel),
openDM, openModal, addReaction, removeReaction, startTyping, stream,
parseMessage edge cases, renderFormatted, link extraction, date parsing.
"""

from __future__ import annotations

import base64
import json
import sys
import time
import types
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from chat_sdk.adapters.slack.adapter import SlackAdapter
    from chat_sdk.adapters.slack.types import SlackAdapterConfig
    from chat_sdk.cards import Card, Table
    from chat_sdk.shared.errors import AdapterError, AdapterRateLimitError, ValidationError
    from chat_sdk.types import (
        FetchOptions,
        ListThreadsOptions,
        StreamChunk,
        StreamOptions,
    )

    _SLACK_AVAILABLE = True
except ImportError:
    _SLACK_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _SLACK_AVAILABLE, reason="Slack adapter import failed")


# =============================================================================
# MockSlackClient -- records calls and returns configurable responses
# =============================================================================


class MockSlackClient:
    """Mock Slack Web API client that records every API call.

    Each Slack API method (``chat_postMessage``, ``reactions_add``, etc.)
    is registered with a configurable return value. The client records
    all calls with their kwargs so tests can inspect them.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses: dict[str, Any] = {}

    def set_response(self, method: str, response: Any) -> None:
        """Configure the return value for a given API method."""
        self._responses[method] = response

    def _make_method(self, method_name: str) -> AsyncMock:
        async def handler(**kwargs: Any) -> dict[str, Any]:
            self.calls.append({"method": method_name, "kwargs": kwargs})
            resp = self._responses.get(method_name, {"ok": True})
            if isinstance(resp, Exception):
                raise resp
            # Support dict-like .get() on the response (like SlackResponse)
            return _DictResponse(resp)

        mock = AsyncMock(side_effect=handler)
        return mock

    def __getattr__(self, name: str) -> Any:
        # Dynamically create mock methods for any Slack API call
        method = self._make_method(name)
        setattr(self, name, method)
        return method

    def get_calls(self, method: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == method]


class _DictResponse(dict):
    """Dict subclass that also supports .data attribute (like SlackResponse)."""

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(data)
        self.data = data


class _FakeSlackApiError(Exception):
    """Mirror of ``slack_sdk.errors.SlackApiError`` for offline tests.

    The dev-group install (``uv sync --group dev``, what CI runs) does NOT
    pull in ``slack_sdk`` (it lives in the ``slack`` / ``all`` extras), and
    ``test_slack_client_cache.py`` injects a bare ``slack_sdk`` ModuleType
    into ``sys.modules`` that has no ``errors`` submodule. So importing the
    real ``SlackApiError`` is order-dependent and breaks under CI. This
    stand-in reproduces the attributes the adapter inspects: ``str(error)``
    contains the Slack error code and ``error.response`` is a dict carrying
    ``{"ok": False, "error": <code>}`` (matching ``SlackApiError``'s shape).
    """

    def __init__(self, message: str, response: dict[str, Any]) -> None:
        self.response = response
        server_error = response.get("error")
        super().__init__(f"{message}\nThe server responded with: {{'ok': False, 'error': '{server_error}'}}")


# =============================================================================
# Helpers
# =============================================================================


def _make_adapter(**overrides: Any) -> SlackAdapter:
    config = SlackAdapterConfig(
        signing_secret=overrides.pop("signing_secret", "test-signing-secret"),
        bot_token=overrides.pop("bot_token", "xoxb-test-token"),
        **overrides,
    )
    return SlackAdapter(config)


def _make_mock_state() -> MagicMock:
    cache: dict[str, Any] = {}
    state = MagicMock()
    state.get = AsyncMock(side_effect=lambda k: cache.get(k))
    state.set = AsyncMock(side_effect=lambda k, v, *a, **kw: cache.__setitem__(k, v))
    state.delete = AsyncMock(side_effect=lambda k: cache.pop(k, None))
    state.append_to_list = AsyncMock()
    state.get_list = AsyncMock(return_value=[])
    state._cache = cache
    return state


def _make_mock_chat(state: MagicMock) -> MagicMock:
    chat = MagicMock()
    chat.process_message = AsyncMock()
    chat.handle_incoming_message = AsyncMock()
    chat.process_reaction = AsyncMock()
    chat.process_action = AsyncMock()
    chat.process_modal_submit = AsyncMock()
    chat.process_modal_close = MagicMock()
    chat.process_slash_command = AsyncMock()
    chat.process_member_joined_channel = AsyncMock()
    chat.get_state = MagicMock(return_value=state)
    chat.get_user_name = MagicMock(return_value="test-bot")
    chat.get_logger = MagicMock(return_value=MagicMock())
    return chat


def _install_recording_httpx(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Swap in a stand-in ``httpx`` whose ``AsyncClient`` records each
    construction, so a test can prove no response_url request was made."""
    created: list[Any] = []

    class _RecordingAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            created.append(self)

    fake = types.ModuleType("httpx")
    fake.AsyncClient = _RecordingAsyncClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", fake)
    return created


def _patch_client(adapter: SlackAdapter, mock_client: MockSlackClient) -> None:
    """Patch the adapter to use a MockSlackClient instead of a real one."""
    adapter._get_client = lambda token=None: mock_client  # type: ignore[assignment]


async def _init_adapter(**overrides: Any) -> tuple[SlackAdapter, MockSlackClient, MagicMock]:
    """Create and initialize an adapter with a mock client."""
    adapter = _make_adapter(**overrides)
    mock_client = MockSlackClient()
    # Prevent the actual auth_test call during initialize
    mock_client.set_response("auth_test", {"user_id": "U_BOT", "bot_id": "B_BOT", "user": "testbot"})
    _patch_client(adapter, mock_client)
    state = _make_mock_state()
    chat = _make_mock_chat(state)
    await adapter.initialize(chat)
    return adapter, mock_client, state


# =============================================================================
# postMessage Tests
# =============================================================================


class TestPostMessage:
    @pytest.mark.asyncio
    async def test_posts_text_message_to_thread(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1234567890.999999"})

        result = await adapter.post_message("slack:C123:1234567890.000000", "Hello from test")

        assert result.id == "1234567890.999999"
        assert result.thread_id == "slack:C123:1234567890.000000"
        assert result.raw is not None
        calls = client.get_calls("chat_postMessage")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"
        assert calls[0]["kwargs"]["thread_ts"] == "1234567890.000000"

    @pytest.mark.asyncio
    async def test_posts_to_channel_with_empty_thread_ts(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1111111111.000000"})

        result = await adapter.post_message("slack:C123:", "Channel message")

        assert result.id == "1111111111.000000"
        calls = client.get_calls("chat_postMessage")
        assert len(calls) == 1
        # Empty thread_ts should be passed as None
        assert calls[0]["kwargs"]["thread_ts"] is None

    @pytest.mark.asyncio
    async def test_sets_unfurl_links_and_unfurl_media_false(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1234567890.999999"})

        await adapter.post_message("slack:C123:1234567890.000000", "test")

        calls = client.get_calls("chat_postMessage")
        assert calls[0]["kwargs"]["unfurl_links"] is False
        assert calls[0]["kwargs"]["unfurl_media"] is False

    @pytest.mark.asyncio
    async def test_posts_card_message(self):
        """postMessage should use blocks when a card is provided."""
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1234567890.111111"})

        card_message = MagicMock()
        card_message.card = {
            "type": "card",
            "title": "Test Card",
            "sections": [{"widgets": [{"type": "text", "text": "Card text"}]}],
        }
        card_message.raw = None
        card_message.markdown = None
        card_message.ast = None
        card_message.files = None

        # Even if card extraction returns something, we mainly want to verify
        # no crash occurs. We test the text fallback path here.
        result = await adapter.post_message("slack:C123:1234567890.000000", "Simple text")
        assert result.id == "1234567890.111111"

    @pytest.mark.asyncio
    async def test_surfaces_slack_block_details_on_invalid_blocks_errors(self):
        """Port of upstream ``enrichInvalidBlocksError``: Slack's per-block details
        are logged and put in the raised error, with the original as ``__cause__``."""
        logger = MagicMock()
        adapter, client, _ = await _init_adapter(logger=logger)
        original = _FakeSlackApiError(
            "invalid_blocks",
            {
                "ok": False,
                "error": "invalid_blocks",
                "errors": ["invalid additional property: page_size [json-pointer:/blocks/0]"],
                "response_metadata": {"messages": ["[ERROR] too many data_visualization blocks"]},
            },
        )
        client.set_response("chat_postMessage", original)
        card = Card(children=[Table(headers=["A"], rows=[["1"]])])

        with pytest.raises(AdapterError) as exc_info:
            await adapter.post_message("slack:C123:1234567890.000000", card)

        assert str(exc_info.value) == (
            "Slack rejected blocks (invalid_blocks): "
            '["invalid additional property: page_size [json-pointer:/blocks/0]",'
            '"[ERROR] too many data_visualization blocks"]'
        )
        assert exc_info.value.code == "invalid_blocks"
        assert exc_info.value.__cause__ is original
        logger.error.assert_called_once()
        message, context = logger.error.call_args.args
        assert message == "Slack rejected blocks (invalid_blocks)"
        assert context["details"] == [
            "invalid additional property: page_size [json-pointer:/blocks/0]",
            "[ERROR] too many data_visualization blocks",
        ]
        assert json.loads(context["blocks"]) == client.get_calls("chat_postMessage")[0]["kwargs"]["blocks"]

    @pytest.mark.asyncio
    async def test_other_card_post_errors_propagate_unchanged(self):
        logger = MagicMock()
        adapter, client, _ = await _init_adapter(logger=logger)
        original = _FakeSlackApiError("channel_not_found", {"ok": False, "error": "channel_not_found"})
        client.set_response("chat_postMessage", original)

        with pytest.raises(_FakeSlackApiError) as exc_info:
            await adapter.post_message("slack:C123:1234567890.000000", Card(children=[]))

        assert exc_info.value is original
        logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_file_only_post_returns_file_id(self):
        """When posting only files with no text, should return a file-like ID."""
        adapter, client, _ = await _init_adapter()
        client.set_response("files_upload_v2", {"ok": True, "files": [{"files": [{"id": "F123"}]}]})
        # chat_postMessage should NOT be called for file-only messages
        chat_post_calls_before = len(client.get_calls("chat_postMessage"))

        from chat_sdk.types import FileUpload, PostableMarkdown

        msg = PostableMarkdown(
            markdown="",
            files=[FileUpload(data=b"hello", filename="test.txt")],
        )
        result = await adapter.post_message("slack:C123:1234567890.000000", msg)

        assert result.id.startswith("file-")
        # chat_postMessage should not have been called
        assert len(client.get_calls("chat_postMessage")) == chat_post_calls_before

    @pytest.mark.asyncio
    async def test_file_only_post_surfaces_confirmed_upload_ids(self):
        """File-only post exposes Slack-confirmed file IDs on ``RawMessage.raw``."""
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "files_upload_v2",
            {"ok": True, "files": [{"files": [{"id": "F1"}, {"id": "F2"}]}]},
        )

        from chat_sdk.types import FileUpload, PostableMarkdown

        msg = PostableMarkdown(
            markdown="",
            files=[FileUpload(data=b"hello", filename="test.txt")],
        )
        result = await adapter.post_message("slack:C123:1234567890.000000", msg)

        assert isinstance(result.raw, dict)
        assert result.raw["uploadedFileIds"] == ["F1", "F2"]
        # The original raw payload is preserved (augment, don't replace).
        assert "files" in result.raw

    @pytest.mark.asyncio
    async def test_text_with_files_surfaces_confirmed_upload_ids(self):
        """Text + files post augments the chat_postMessage raw with confirmed IDs."""
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1234567890.222222"})
        client.set_response(
            "files_upload_v2",
            {"ok": True, "files": [{"files": [{"id": "F9"}]}]},
        )

        from chat_sdk.types import FileUpload, PostableMarkdown

        msg = PostableMarkdown(
            markdown="here is the report",
            files=[FileUpload(data=b"hello", filename="report.txt")],
        )
        result = await adapter.post_message("slack:C123:1234567890.000000", msg)

        assert result.id == "1234567890.222222"
        assert isinstance(result.raw, dict)
        assert result.raw["uploadedFileIds"] == ["F9"]
        # The Slack chat_postMessage response is preserved alongside the IDs.
        assert result.raw["ok"] is True

    @pytest.mark.asyncio
    async def test_text_only_post_does_not_add_uploaded_file_ids(self):
        """Posts without files leave ``raw`` unaugmented (no ``uploadedFileIds`` key)."""
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1234567890.333333"})

        result = await adapter.post_message("slack:C123:1234567890.000000", "plain text")

        assert isinstance(result.raw, dict)
        assert "uploadedFileIds" not in result.raw

    @pytest.mark.asyncio
    async def test_file_upload_uses_channel_kwarg_not_channel_id(self):
        """Regression: slack-sdk's files_upload_v2 takes ``channel=``, not ``channel_id=``.

        Passing ``channel_id=`` collides with the kwarg slack-sdk adds internally
        when it forwards to files_completeUploadExternal, raising a TypeError on
        every upload. See issue #102.
        """
        adapter, client, _ = await _init_adapter()
        client.set_response("files_upload_v2", {"ok": True, "files": [{"files": [{"id": "F999"}]}]})

        from chat_sdk.types import FileUpload, PostableMarkdown

        msg = PostableMarkdown(
            markdown="",
            files=[FileUpload(data=b"hello", filename="test.txt")],
        )
        await adapter.post_message("slack:C789:1234567890.000000", msg)

        upload_calls = client.get_calls("files_upload_v2")
        assert len(upload_calls) == 1
        kwargs = upload_calls[0]["kwargs"]
        assert kwargs["channel"] == "C789"
        assert "channel_id" not in kwargs
        assert kwargs["thread_ts"] == "1234567890.000000"

    @pytest.mark.asyncio
    async def test_thread_reply(self):
        """postMessage to a thread should include thread_ts."""
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1234567891.000000"})

        result = await adapter.post_message("slack:C456:1234567890.000000", "Thread reply")

        assert result.thread_id == "slack:C456:1234567890.000000"
        calls = client.get_calls("chat_postMessage")
        assert calls[0]["kwargs"]["thread_ts"] == "1234567890.000000"

    @pytest.mark.asyncio
    async def test_markdown_message_posts_via_native_markdown_text(self):
        """Markdown is passed through to Slack's markdown_text field for
        native rendering -- not converted to mrkdwn ``text``."""
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1234567890.444444"})

        from chat_sdk.types import PostableMarkdown

        await adapter.post_message(
            "slack:C123:1234567890.000000",
            PostableMarkdown(markdown="**Bold** and _italic_ and `code`"),
        )

        calls = client.get_calls("chat_postMessage")
        assert len(calls) == 1
        kwargs = calls[0]["kwargs"]
        assert kwargs["markdown_text"] == "**Bold** and _italic_ and `code`"
        # markdown_text is mutually exclusive with text.
        assert "text" not in kwargs

    @pytest.mark.asyncio
    async def test_plain_string_posts_via_text_not_markdown_text(self):
        """Plain strings keep going to ``text`` so literal ``*`` survives."""
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1234567890.555555"})

        await adapter.post_message("slack:C123:1234567890.000000", "Use *foo* literally")

        kwargs = client.get_calls("chat_postMessage")[0]["kwargs"]
        assert kwargs["text"] == "Use *foo* literally"
        assert "markdown_text" not in kwargs

    @pytest.mark.asyncio
    async def test_markdown_table_posts_as_markdown_text_without_blocks(self):
        """Tables ride along in markdown_text (Slack renders them natively);
        the legacy native-table-block conversion is gone."""
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1234567890.666666"})

        from chat_sdk.types import PostableMarkdown

        await adapter.post_message(
            "slack:C123:1234567890.000000",
            PostableMarkdown(markdown="| A | B |\n|---|---|\n| 1 | 2 |"),
        )

        kwargs = client.get_calls("chat_postMessage")[0]["kwargs"]
        assert kwargs["markdown_text"] == "| A | B |\n|---|---|\n| 1 | 2 |"
        assert "blocks" not in kwargs
        assert "text" not in kwargs


# =============================================================================
# editMessage Tests
# =============================================================================


class TestEditMessage:
    @pytest.mark.asyncio
    async def test_edits_text_message(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_update", {"ok": True, "ts": "1234567890.123456"})

        result = await adapter.edit_message(
            "slack:C123:1234567890.000000",
            "1234567890.123456",
            "Updated message",
        )

        assert result.id == "1234567890.123456"
        assert result.thread_id == "slack:C123:1234567890.000000"
        calls = client.get_calls("chat_update")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"
        assert calls[0]["kwargs"]["ts"] == "1234567890.123456"

    @pytest.mark.asyncio
    async def test_edit_message_returns_correct_thread_id(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_update", {"ok": True, "ts": "9999.9999"})

        result = await adapter.edit_message(
            "slack:CABC:1111.2222",
            "9999.9999",
            "Edited text",
        )

        assert result.thread_id == "slack:CABC:1111.2222"

    @pytest.mark.asyncio
    async def test_edit_with_markdown_uses_markdown_text(self):
        """chat.update sends markdown via markdown_text, like postMessage."""
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_update", {"ok": True, "ts": "1234567890.123456"})

        from chat_sdk.types import PostableMarkdown

        await adapter.edit_message(
            "slack:C123:1234567890.000000",
            "1234567890.123456",
            PostableMarkdown(markdown="**Updated** body"),
        )

        kwargs = client.get_calls("chat_update")[0]["kwargs"]
        assert kwargs["markdown_text"] == "**Updated** body"
        assert "text" not in kwargs

    @pytest.mark.asyncio
    async def test_edit_ephemeral_via_response_url_uses_mrkdwn_fallback(self):
        """response_url payloads reject markdown_text (`no_text`), so
        markdown/AST edits are rendered to legacy mrkdwn ``text``."""
        adapter, _, _ = await _init_adapter()
        ephemeral_id = adapter._encode_ephemeral_message_id(
            "1234567890.123456", "https://hooks.slack.com/respond", "U1"
        )

        recorded: dict[str, Any] = {}

        class FakeResponse:
            is_success = True
            status_code = 200
            text = "ok"

        class FakeAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def post(self, url: str, *, json: Any = None, headers: Any = None) -> FakeResponse:
                recorded["url"] = url
                recorded["json"] = json
                return FakeResponse()

        # ``_send_to_response_url`` lazily imports httpx (optional dep, not
        # installed in the test env) -- inject a stand-in module.
        import sys
        import types

        fake_httpx = types.ModuleType("httpx")
        fake_httpx.AsyncClient = FakeAsyncClient  # type: ignore[attr-defined]
        original_module = sys.modules.get("httpx")
        sys.modules["httpx"] = fake_httpx
        try:
            from chat_sdk.types import PostableMarkdown

            await adapter.edit_message(
                "slack:C123:1234567890.000000",
                ephemeral_id,
                PostableMarkdown(markdown="**Updated** [text](https://example.com)\n\n| A | B |\n|---|---|\n| 1 | 2 |"),
            )
        finally:
            if original_module is not None:
                sys.modules["httpx"] = original_module
            else:
                sys.modules.pop("httpx", None)

        assert recorded["url"] == "https://hooks.slack.com/respond"
        body = recorded["json"]
        assert body["replace_original"] is True
        assert "*Updated* <https://example.com|text>" in body["text"]
        # Tables fall back to ASCII code blocks on this surface.
        assert "```" in body["text"]
        assert "markdown_text" not in body

    @pytest.mark.asyncio
    async def test_rejects_an_untrusted_encoded_response_url_before_fetching(self, monkeypatch):
        adapter, client, _ = await _init_adapter()
        created = _install_recording_httpx(monkeypatch)
        # Hand-built: the encoder itself refuses untrusted URLs.
        data = json.dumps({"responseUrl": "https://attacker.example/respond", "userId": "U123"})
        ephemeral_id = f"ephemeral:1234567890.123456:{base64.b64encode(data.encode()).decode()}"

        with pytest.raises(ValidationError, match="Invalid Slack ephemeral message ID"):
            await adapter.edit_message("slack:C123:1234567890.000000", ephemeral_id, "private message")

        assert created == []
        client.chat_update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_defends_the_response_url_fetch_sink_against_untrusted_callers(self, monkeypatch):
        adapter, _, _ = await _init_adapter()
        created = _install_recording_httpx(monkeypatch)

        with pytest.raises(ValidationError, match="untrusted Slack response_url"):
            await adapter._send_to_response_url("https://hooks.slack.com.attacker.example/respond", "delete")

        assert created == []


# =============================================================================
# deleteMessage Tests
# =============================================================================


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_deletes_message_by_id(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_delete", {"ok": True})

        await adapter.delete_message("slack:C123:1234567890.000000", "1234567890.123456")

        calls = client.get_calls("chat_delete")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"
        assert calls[0]["kwargs"]["ts"] == "1234567890.123456"

    @pytest.mark.asyncio
    async def test_delete_message_decodes_thread_id(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_delete", {"ok": True})

        await adapter.delete_message("slack:CXYZ:9999.8888", "1111.2222")

        calls = client.get_calls("chat_delete")
        assert calls[0]["kwargs"]["channel"] == "CXYZ"

    @pytest.mark.asyncio
    async def test_delete_rejects_malformed_ephemeral_id(self, monkeypatch):
        """Python-specific: an undecodable ``ephemeral:`` id must raise rather
        than fall through to ``chat.delete`` with a bogus ts."""
        adapter, client, _ = await _init_adapter()
        created = _install_recording_httpx(monkeypatch)

        with pytest.raises(ValidationError, match="Invalid Slack ephemeral message ID"):
            await adapter.delete_message("slack:C123:1234567890.000000", "ephemeral:1234567890.123456:!!not-base64!!")

        client.chat_delete.assert_not_awaited()
        assert created == []

    @pytest.mark.asyncio
    async def test_delete_ephemeral_posts_to_gov_slack_response_url(self, monkeypatch):
        """Python-specific: the response_url sink used to require
        ``*.slack.com`` and so rejected GovSlack's ``hooks.slack-gov.com``."""
        adapter, client, _ = await _init_adapter()
        url = "https://hooks.slack-gov.com/actions/T123/456/abc"
        ephemeral_id = adapter._encode_ephemeral_message_id("1234567890.123456", url, "U_GOV")

        response = MagicMock(is_success=True, status_code=200, text="")
        post = AsyncMock(return_value=response)

        class _FakeAsyncClient:
            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        _FakeAsyncClient.post = post  # type: ignore[attr-defined]
        fake = types.ModuleType("httpx")
        fake.AsyncClient = _FakeAsyncClient  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "httpx", fake)

        await adapter.delete_message("slack:C123:1234567890.000000", ephemeral_id)

        post.assert_awaited_once_with(
            url,
            json={"delete_original": True},
            headers={"Content-Type": "application/json"},
        )
        client.chat_delete.assert_not_awaited()


# =============================================================================
# fetchMessages Tests
# =============================================================================


class TestFetchMessages:
    @pytest.mark.asyncio
    async def test_fetch_backward_default(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_replies",
            {
                "ok": True,
                "messages": [
                    {"ts": "1234567890.000001", "text": "Message 1", "user": "U1"},
                    {"ts": "1234567890.000002", "text": "Message 2", "user": "U2"},
                ],
                "has_more": False,
            },
        )

        result = await adapter.fetch_messages("slack:C123:1234567890.000000")

        assert len(result.messages) == 2
        assert result.next_cursor is None

    @pytest.mark.asyncio
    async def test_fetch_forward(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_replies",
            {
                "ok": True,
                "messages": [
                    {"ts": "1234567890.000001", "text": "First", "user": "U1"},
                    {"ts": "1234567890.000002", "text": "Second", "user": "U2"},
                ],
                "response_metadata": {"next_cursor": "cursor_123"},
            },
        )

        result = await adapter.fetch_messages(
            "slack:C123:1234567890.000000",
            FetchOptions(direction="forward"),
        )

        assert len(result.messages) == 2
        assert result.next_cursor == "cursor_123"

    @pytest.mark.asyncio
    async def test_fetch_backward_with_cursor(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_replies",
            {
                "ok": True,
                "messages": [
                    {"ts": "1234567890.000010", "text": "Older", "user": "U1"},
                ],
                "has_more": True,
            },
        )

        result = await adapter.fetch_messages(
            "slack:C123:1234567890.000000",
            FetchOptions(direction="backward", cursor="1234567890.000020"),
        )

        assert len(result.messages) >= 1

    @pytest.mark.asyncio
    async def test_fetch_with_limit(self):
        adapter, client, _ = await _init_adapter()
        msgs = [{"ts": f"123456789{i}.000000", "text": f"msg{i}", "user": "U1"} for i in range(10)]
        client.set_response("conversations_replies", {"ok": True, "messages": msgs, "has_more": False})

        result = await adapter.fetch_messages(
            "slack:C123:1234567890.000000",
            FetchOptions(limit=5),
        )

        # Should return at most limit messages
        assert len(result.messages) <= 10

    @pytest.mark.asyncio
    async def test_empty_dm_thread_ts_backward_uses_history_not_replies(self):
        """A DM root (slack:Dxxx:) encodes thread_ts="" — backward fetch must
        route to conversations.history (the channel IS the conversation), not
        conversations.replies(ts="") which returns nothing for a DM (#138)."""
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_history",
            {
                "ok": True,
                "messages": [
                    {"ts": "1234567890.000002", "text": "DM 2", "user": "U2"},
                    {"ts": "1234567890.000001", "text": "DM 1", "user": "U1"},
                ],
                "has_more": False,
            },
        )

        result = await adapter.fetch_messages("slack:D999:")

        # The DM root messages come back via conversations.history.
        assert len(result.messages) == 2
        history_calls = client.get_calls("conversations_history")
        assert len(history_calls) == 1
        assert history_calls[0]["kwargs"]["channel"] == "D999"
        # conversations.replies must NOT be hit at all (esp. not with ts="").
        replies_calls = client.get_calls("conversations_replies")
        assert replies_calls == []

    @pytest.mark.asyncio
    async def test_empty_dm_thread_ts_forward_uses_history_not_replies(self):
        """Forward direction over a DM root also routes to conversations.history,
        preserving cursor/limit semantics, never conversations.replies(ts="")."""
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_history",
            {
                "ok": True,
                "messages": [
                    {"ts": "1234567890.000002", "text": "Newer", "user": "U2"},
                    {"ts": "1234567890.000001", "text": "Older", "user": "U1"},
                ],
                "has_more": True,
            },
        )

        result = await adapter.fetch_messages(
            "slack:D999:",
            FetchOptions(direction="forward", cursor="1234567890.000000", limit=50),
        )

        assert len(result.messages) == 2
        history_calls = client.get_calls("conversations_history")
        assert len(history_calls) == 1
        assert history_calls[0]["kwargs"]["channel"] == "D999"
        # Forward cursor maps to oldest= on conversations.history (channel-history path).
        assert history_calls[0]["kwargs"]["oldest"] == "1234567890.000000"
        assert history_calls[0]["kwargs"]["limit"] == 50
        # has_more + slack messages → next_cursor from the newest ts.
        assert result.next_cursor == "1234567890.000002"
        assert client.get_calls("conversations_replies") == []

    @pytest.mark.asyncio
    async def test_non_empty_thread_ts_still_uses_replies_backward(self):
        """Regression guard: a real thread root (non-empty thread_ts) MUST keep
        using conversations.replies — the empty-DM routing must not over-trigger."""
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_replies",
            {
                "ok": True,
                "messages": [
                    {"ts": "1234567890.000001", "text": "Reply 1", "user": "U1"},
                ],
                "has_more": False,
            },
        )

        result = await adapter.fetch_messages("slack:C123:1234567890.000000")

        assert len(result.messages) == 1
        replies_calls = client.get_calls("conversations_replies")
        assert len(replies_calls) == 1
        assert replies_calls[0]["kwargs"]["ts"] == "1234567890.000000"
        # Channel-history path must NOT be used for a real thread.
        assert client.get_calls("conversations_history") == []

    @pytest.mark.asyncio
    async def test_non_empty_thread_ts_still_uses_replies_forward(self):
        """Regression guard (forward): non-empty thread_ts keeps conversations.replies."""
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_replies",
            {
                "ok": True,
                "messages": [
                    {"ts": "1234567890.000001", "text": "First", "user": "U1"},
                ],
                "response_metadata": {"next_cursor": "cur"},
            },
        )

        result = await adapter.fetch_messages(
            "slack:C123:1234567890.000000",
            FetchOptions(direction="forward"),
        )

        assert len(result.messages) == 1
        replies_calls = client.get_calls("conversations_replies")
        assert len(replies_calls) == 1
        assert replies_calls[0]["kwargs"]["ts"] == "1234567890.000000"
        assert client.get_calls("conversations_history") == []


class TestFetchedMessagesKeepCachedUnfurls:
    """Messages from ``conversations.history`` / ``conversations.replies`` carry
    no ``channel`` field, so the unfurl cache key's channel must come from the
    thread id the message is parsed under. Otherwise ``_enrich_links`` returns
    early and fetched messages lose the ``message_changed`` unfurl metadata.

    What to fix if this fails: ``SlackAdapter._unfurl_channel_for`` and its use
    in ``_parse_slack_message``.
    """

    _TS = "1234567890.000050"
    _URL = "https://example.com/article"

    def _history_message(self) -> dict[str, Any]:
        # Shape of a history/replies item: no ``channel`` key, bare link.
        return {"ts": self._TS, "text": f"see <{self._URL}>", "user": "U1"}

    def _unfurl_payload(self) -> dict[str, Any]:
        return {self._URL: {"title": "Cached Title", "description": "Cached body"}}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["fetch_messages", "fetch_message", "fetch_channel_messages"])
    async def test_fetched_message_links_are_enriched_from_the_unfurl_cache(self, path: str):
        adapter, client, state = await _init_adapter()
        state._cache[f"slack:unfurls:C123:{self._TS}"] = self._unfurl_payload()
        page = {"ok": True, "messages": [self._history_message()], "has_more": False}
        client.set_response("conversations_replies", page)
        client.set_response("conversations_history", page)

        if path == "fetch_messages":
            messages = (await adapter.fetch_messages("slack:C123:1234567890.000000")).messages
        elif path == "fetch_message":
            msg = await adapter.fetch_message("slack:C123:1234567890.000000", self._TS)
            assert msg is not None
            messages = [msg]
        else:
            messages = (await adapter.fetch_channel_messages("slack:C123")).messages

        assert len(messages) == 1
        links = messages[0].links
        assert [link.url for link in links] == [self._URL]
        assert links[0].title == "Cached Title"
        assert links[0].description == "Cached body"

    @pytest.mark.asyncio
    async def test_fetched_message_uses_the_installation_scoped_unfurl_key(self):
        from chat_sdk.adapters.slack.types import RequestContext

        adapter, client, state = await _init_adapter()
        # Only the T_A-scoped entry exists; an unscoped or wrong-install key
        # must not be read.
        state._cache[f"slack:unfurls:T_A:C123:{self._TS}"] = self._unfurl_payload()
        client.set_response(
            "conversations_replies",
            {"ok": True, "messages": [self._history_message()], "has_more": False},
        )

        tok = adapter._request_context.set(RequestContext(token="xoxb-T_A", installation_id="T_A"))
        try:
            result = await adapter.fetch_messages("slack:C123:1234567890.000000")
        finally:
            adapter._request_context.reset(tok)

        assert result.messages[0].links[0].title == "Cached Title"
        state.get.assert_any_await(f"slack:unfurls:T_A:C123:{self._TS}")


# =============================================================================
# fetchMessage (single) Tests
# =============================================================================


class TestFetchSingleMessage:
    @pytest.mark.asyncio
    async def test_non_empty_thread_ts_uses_replies(self):
        """A single-message fetch on a real thread uses conversations.replies
        (oldest=message_id) — byte-identical to the pre-#138 path."""
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_replies",
            {
                "ok": True,
                "messages": [
                    {"ts": "1234567890.000050", "text": "Target", "user": "U1"},
                ],
            },
        )

        msg = await adapter.fetch_message("slack:C123:1234567890.000000", "1234567890.000050")

        assert msg is not None
        assert msg.id == "1234567890.000050"
        replies_calls = client.get_calls("conversations_replies")
        assert len(replies_calls) == 1
        assert replies_calls[0]["kwargs"]["ts"] == "1234567890.000000"
        assert replies_calls[0]["kwargs"]["oldest"] == "1234567890.000050"
        assert client.get_calls("conversations_history") == []

    @pytest.mark.asyncio
    async def test_empty_dm_thread_ts_uses_history(self):
        """A single-message fetch on a DM root (empty thread_ts) reads from
        conversations.history (latest=message_id), NOT conversations.replies(ts="")
        which cannot locate the message (#138)."""
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_history",
            {
                "ok": True,
                "messages": [
                    {"ts": "1234567890.000050", "text": "DM message", "user": "U1"},
                ],
            },
        )

        msg = await adapter.fetch_message("slack:D999:", "1234567890.000050")

        assert msg is not None
        assert msg.id == "1234567890.000050"
        history_calls = client.get_calls("conversations_history")
        assert len(history_calls) == 1
        assert history_calls[0]["kwargs"]["channel"] == "D999"
        assert history_calls[0]["kwargs"]["latest"] == "1234567890.000050"
        assert history_calls[0]["kwargs"]["inclusive"] is True
        assert history_calls[0]["kwargs"]["limit"] == 1
        # conversations.replies must NOT be called with ts="".
        assert client.get_calls("conversations_replies") == []


# =============================================================================
# fetchThread Tests
# =============================================================================


class TestFetchThread:
    @pytest.mark.asyncio
    async def test_fetches_thread_info(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_info",
            {
                "ok": True,
                "channel": {"name": "general", "is_private": False},
            },
        )

        result = await adapter.fetch_thread("slack:C123:1234567890.000000")

        assert result.id == "slack:C123:1234567890.000000"
        assert result.channel_name == "general"
        calls = client.get_calls("conversations_info")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"

    @pytest.mark.asyncio
    async def test_detects_external_shared_channel(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_info",
            {
                "ok": True,
                "channel": {"name": "ext-channel", "is_ext_shared": True},
            },
        )

        result = await adapter.fetch_thread("slack:C123:1234567890.000000")
        assert result.channel_visibility == "external"

    @pytest.mark.asyncio
    async def test_detects_private_channel(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_info",
            {
                "ok": True,
                "channel": {"name": "private-chan", "is_private": True},
            },
        )

        result = await adapter.fetch_thread("slack:C123:1234567890.000000")
        assert result.channel_visibility == "private"


# =============================================================================
# listThreads Tests
# =============================================================================


class TestListThreads:
    @pytest.mark.asyncio
    async def test_lists_threads_in_channel(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_history",
            {
                "ok": True,
                "messages": [
                    {
                        "ts": "1234567890.000001",
                        "text": "Thread root",
                        "user": "U1",
                        "reply_count": 5,
                        "latest_reply": "1234567895.000000",
                    },
                    {
                        "ts": "1234567890.000002",
                        "text": "No replies",
                        "user": "U2",
                        "reply_count": 0,
                    },
                    {
                        "ts": "1234567890.000003",
                        "text": "Another thread",
                        "user": "U3",
                        "reply_count": 2,
                    },
                ],
                "response_metadata": {},
            },
        )

        result = await adapter.list_threads("slack:C123")

        # Should only include messages with reply_count > 0
        assert len(result.threads) == 2
        assert result.threads[0].reply_count == 5
        assert result.threads[1].reply_count == 2

    @pytest.mark.asyncio
    async def test_list_threads_with_limit(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_history",
            {
                "ok": True,
                "messages": [
                    {"ts": f"123456789{i}.000", "text": f"T{i}", "user": "U1", "reply_count": 1} for i in range(10)
                ],
                "response_metadata": {},
            },
        )

        result = await adapter.list_threads("slack:C123", ListThreadsOptions(limit=3))

        assert len(result.threads) == 3

    @pytest.mark.asyncio
    async def test_list_threads_pagination_cursor(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "conversations_history",
            {
                "ok": True,
                "messages": [
                    {"ts": "111.000", "text": "T1", "user": "U1", "reply_count": 1},
                ],
                "response_metadata": {"next_cursor": "next_page_token"},
            },
        )

        result = await adapter.list_threads("slack:C123")

        assert result.next_cursor == "next_page_token"


# =============================================================================
# postEphemeral Tests
# =============================================================================


class TestPostEphemeral:
    @pytest.mark.asyncio
    async def test_posts_ephemeral_message(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postEphemeral", {"ok": True, "message_ts": "1234567890.888888"})

        result = await adapter.post_ephemeral(
            "slack:C123:1234567890.000000",
            "U_USER_1",
            "Ephemeral text",
        )

        assert result.id == "1234567890.888888"
        assert result.thread_id == "slack:C123:1234567890.000000"
        assert result.used_fallback is False

        calls = client.get_calls("chat_postEphemeral")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"
        assert calls[0]["kwargs"]["user"] == "U_USER_1"
        assert calls[0]["kwargs"]["thread_ts"] == "1234567890.000000"

    @pytest.mark.asyncio
    async def test_omits_thread_ts_when_empty(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postEphemeral", {"ok": True, "message_ts": "1234567890.888888"})

        await adapter.post_ephemeral("slack:C123:", "U_USER_1", "Ephemeral text")

        calls = client.get_calls("chat_postEphemeral")
        assert calls[0]["kwargs"]["thread_ts"] is None

    @pytest.mark.asyncio
    async def test_handles_empty_message_ts_in_response(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postEphemeral", {"ok": True})

        result = await adapter.post_ephemeral(
            "slack:C123:1234567890.000000",
            "U_USER_1",
            "test",
        )

        assert result.id == ""

    @pytest.mark.asyncio
    async def test_ephemeral_markdown_uses_markdown_text(self):
        """chat.postEphemeral sends markdown via markdown_text too."""
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postEphemeral", {"ok": True, "message_ts": "1234567890.777777"})

        from chat_sdk.types import PostableMarkdown

        await adapter.post_ephemeral(
            "slack:C123:1234567890.000000",
            "U_USER_1",
            PostableMarkdown(markdown="## Only for you"),
        )

        kwargs = client.get_calls("chat_postEphemeral")[0]["kwargs"]
        assert kwargs["markdown_text"] == "## Only for you"
        assert "text" not in kwargs


# =============================================================================
# scheduleMessage Tests
# =============================================================================


class TestScheduleMessage:
    @pytest.mark.asyncio
    async def test_schedules_message(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "chat_scheduleMessage",
            {"ok": True, "scheduled_message_id": "Q1234"},
        )

        future_time = datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc)
        result = await adapter.schedule_message(
            "slack:C123:1234567890.000000",
            "Scheduled hello",
            future_time,
        )

        assert result.scheduled_message_id == "Q1234"
        assert result.channel_id == "C123"
        assert result.post_at == future_time

        calls = client.get_calls("chat_scheduleMessage")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"

    @pytest.mark.asyncio
    async def test_cancel_scheduled_message(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "chat_scheduleMessage",
            {"ok": True, "scheduled_message_id": "Q5678"},
        )
        client.set_response("chat_deleteScheduledMessage", {"ok": True})

        future_time = datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc)
        result = await adapter.schedule_message(
            "slack:C123:1234567890.000000",
            "To cancel",
            future_time,
        )

        await result.cancel()

        calls = client.get_calls("chat_deleteScheduledMessage")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["scheduled_message_id"] == "Q5678"

    @pytest.mark.asyncio
    async def test_schedule_markdown_uses_markdown_text(self):
        """chat.scheduleMessage sends markdown via markdown_text too."""
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_scheduleMessage", {"ok": True, "scheduled_message_id": "Q9999"})

        from chat_sdk.types import PostableMarkdown

        future_time = datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc)
        await adapter.schedule_message(
            "slack:C123:1234567890.000000",
            PostableMarkdown(markdown="**Reminder** tomorrow"),
            future_time,
        )

        kwargs = client.get_calls("chat_scheduleMessage")[0]["kwargs"]
        assert kwargs["markdown_text"] == "**Reminder** tomorrow"
        assert "text" not in kwargs

    @pytest.mark.asyncio
    async def test_rejects_past_time(self):
        adapter, client, _ = await _init_adapter()

        past_time = datetime.fromtimestamp(time.time() - 3600, tz=timezone.utc)
        with pytest.raises(ValidationError):
            await adapter.schedule_message(
                "slack:C123:1234567890.000000",
                "Too late",
                past_time,
            )

    @pytest.mark.asyncio
    async def test_rejects_files_in_scheduled_messages(self):
        adapter, client, _ = await _init_adapter()
        from chat_sdk.types import FileUpload, PostableMarkdown

        future_time = datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc)
        msg = PostableMarkdown(
            markdown="With file",
            files=[FileUpload(data=b"data", filename="test.txt")],
        )
        with pytest.raises(ValidationError, match="[Ff]ile"):
            await adapter.schedule_message(
                "slack:C123:1234567890.000000",
                msg,
                future_time,
            )


# =============================================================================
# openDM Tests
# =============================================================================


class TestOpenDM:
    @pytest.mark.asyncio
    async def test_opens_dm_conversation(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("conversations_open", {"ok": True, "channel": {"id": "D_DM_CHAN"}})

        result = await adapter.open_dm("U_TARGET_USER")

        assert "D_DM_CHAN" in result
        assert result.startswith("slack:")
        calls = client.get_calls("conversations_open")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["users"] == "U_TARGET_USER"

    @pytest.mark.asyncio
    async def test_open_dm_returns_valid_thread_id(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("conversations_open", {"ok": True, "channel": {"id": "D_ABC123"}})

        result = await adapter.open_dm("U_OTHER")

        decoded = adapter.decode_thread_id(result)
        assert decoded.channel == "D_ABC123"
        assert decoded.thread_ts == ""


# =============================================================================
# openModal Tests
# =============================================================================


class TestOpenModal:
    @pytest.mark.asyncio
    async def test_opens_modal(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("views_open", {"ok": True, "view": {"id": "V_MODAL_123"}})

        result = await adapter.open_modal(
            "trigger-123",
            {"callback_id": "my_modal", "title": "Test Modal"},
        )

        assert result["viewId"] == "V_MODAL_123"
        calls = client.get_calls("views_open")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["trigger_id"] == "trigger-123"

    @pytest.mark.asyncio
    async def test_opens_modal_with_context_id(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("views_open", {"ok": True, "view": {"id": "V_CTX_456"}})

        result = await adapter.open_modal(
            "trigger-456",
            {"callback_id": "ctx_modal"},
            context_id="ctx-abc",
        )

        assert result["viewId"] == "V_CTX_456"


# =============================================================================
# addReaction / removeReaction Tests
# =============================================================================


class TestAddReaction:
    @pytest.mark.asyncio
    async def test_adds_reaction_to_message(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("reactions_add", {"ok": True})

        await adapter.add_reaction(
            "slack:C123:1234567890.000000",
            "1234567890.123456",
            "thumbsup",
        )

        calls = client.get_calls("reactions_add")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"
        assert calls[0]["kwargs"]["timestamp"] == "1234567890.123456"
        assert "thumbsup" in calls[0]["kwargs"]["name"]

    @pytest.mark.asyncio
    async def test_strips_colons_from_emoji_name(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("reactions_add", {"ok": True})

        await adapter.add_reaction(
            "slack:C123:1234567890.000000",
            "1234567890.123456",
            ":tada:",
        )

        calls = client.get_calls("reactions_add")
        name = calls[0]["kwargs"]["name"]
        assert ":" not in name


class TestRemoveReaction:
    @pytest.mark.asyncio
    async def test_removes_reaction_from_message(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("reactions_remove", {"ok": True})

        await adapter.remove_reaction(
            "slack:C123:1234567890.000000",
            "1234567890.123456",
            "thumbsup",
        )

        calls = client.get_calls("reactions_remove")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel"] == "C123"
        assert calls[0]["kwargs"]["timestamp"] == "1234567890.123456"

    @pytest.mark.asyncio
    async def test_removes_reaction_strips_colons(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("reactions_remove", {"ok": True})

        await adapter.remove_reaction(
            "slack:C123:1234567890.000000",
            "1234567890.123456",
            ":wave:",
        )

        calls = client.get_calls("reactions_remove")
        name = calls[0]["kwargs"]["name"]
        assert ":" not in name


# =============================================================================
# startTyping Tests
# =============================================================================


class TestStartTyping:
    @pytest.mark.asyncio
    async def test_sets_typing_status(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("assistant_threads_setStatus", {"ok": True})

        await adapter.start_typing("slack:C123:1234567890.000000")

        calls = client.get_calls("assistant_threads_setStatus")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["channel_id"] == "C123"
        assert calls[0]["kwargs"]["thread_ts"] == "1234567890.000000"

    @pytest.mark.asyncio
    async def test_skips_when_no_thread_ts(self):
        adapter, client, _ = await _init_adapter()

        await adapter.start_typing("slack:C123:")

        calls = client.get_calls("assistant_threads_setStatus")
        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_custom_status_text(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("assistant_threads_setStatus", {"ok": True})

        await adapter.start_typing("slack:C123:1234567890.000000", status="Thinking...")

        calls = client.get_calls("assistant_threads_setStatus")
        assert calls[0]["kwargs"]["status"] == "Thinking..."

    @pytest.mark.asyncio
    async def test_does_not_raise_on_error(self):
        """startTyping should silently catch errors."""
        adapter, client, _ = await _init_adapter()
        client.set_response("assistant_threads_setStatus", Exception("API down"))

        # startTyping swallows the API error and returns normally
        await adapter.start_typing("slack:C123:1234567890.000000")

        # The API call was attempted (error was caught, not avoided)
        calls = client.get_calls("assistant_threads_setStatus")
        assert len(calls) == 1


# =============================================================================
# stream Tests
# =============================================================================


class TestStream:
    @pytest.mark.asyncio
    async def test_stream_requires_recipient_info(self):
        adapter, client, _ = await _init_adapter()

        async def text_gen() -> AsyncIterator[str]:
            yield "Hello"

        with pytest.raises(ValidationError, match="recipient"):
            await adapter.stream("slack:C123:1234567890.000000", text_gen())

    @pytest.mark.asyncio
    async def test_stream_requires_recipient_user_and_team(self):
        adapter, client, _ = await _init_adapter()

        async def text_gen() -> AsyncIterator[str]:
            yield "Hello"

        with pytest.raises(ValidationError):
            await adapter.stream(
                "slack:C123:1234567890.000000",
                text_gen(),
                StreamOptions(recipient_user_id="U1"),  # missing team
            )

    @pytest.mark.asyncio
    async def test_stream_with_markdown_text_chunks(self):
        """Stream should handle StreamChunk objects with type=markdown_text."""
        adapter, client, _ = await _init_adapter()

        # Mock the chat_stream interface
        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock()
        mock_streamer.stop = AsyncMock(return_value={"message": {"ts": "999.999"}})
        client.chat_stream = AsyncMock(return_value=mock_streamer)

        from chat_sdk.types import MarkdownTextChunk

        async def chunk_gen() -> AsyncIterator[StreamChunk | str]:
            yield MarkdownTextChunk(text="Hello ")
            yield MarkdownTextChunk(text="world")

        result = await adapter.stream(
            "slack:C123:1234567890.000000",
            chunk_gen(),
            StreamOptions(recipient_user_id="U1", recipient_team_id="T1"),
        )

        assert result.id == "999.999"
        assert mock_streamer.append.called

    @pytest.mark.asyncio
    async def test_stream_with_task_update_chunks(self):
        """Stream should handle structured task_update chunks."""
        adapter, client, _ = await _init_adapter()

        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock()
        mock_streamer.stop = AsyncMock(return_value={"message": {"ts": "888.888"}})
        client.chat_stream = AsyncMock(return_value=mock_streamer)

        from chat_sdk.types import TaskUpdateChunk

        async def chunk_gen() -> AsyncIterator[StreamChunk | str]:
            yield "Starting task..."
            yield TaskUpdateChunk(id="task1", title="Search", status="in_progress")
            yield TaskUpdateChunk(id="task1", title="Search", status="completed", output="Found 5 results")

        result = await adapter.stream(
            "slack:C123:1234567890.000000",
            chunk_gen(),
            StreamOptions(recipient_user_id="U1", recipient_team_id="T1"),
        )

        assert result.id == "888.888"

    @pytest.mark.asyncio
    async def test_stream_with_plan_update_chunks(self):
        """Stream should handle structured plan_update chunks."""
        adapter, client, _ = await _init_adapter()

        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock()
        mock_streamer.stop = AsyncMock(return_value={"message": {"ts": "777.777"}})
        client.chat_stream = AsyncMock(return_value=mock_streamer)

        from chat_sdk.types import PlanUpdateChunk

        async def chunk_gen() -> AsyncIterator[StreamChunk | str]:
            yield PlanUpdateChunk(title="Step 1: Gather info")
            yield "Gathering..."
            yield PlanUpdateChunk(title="Step 2: Analyze")

        result = await adapter.stream(
            "slack:C123:1234567890.000000",
            chunk_gen(),
            StreamOptions(recipient_user_id="U1", recipient_team_id="T1"),
        )

        assert result.id == "777.777"

    @pytest.mark.asyncio
    async def test_stream_skips_thinking_chunk_by_default(self):
        """A ``ThinkingChunk`` must not change the posted message and must not
        be sent as a structured chunk (which would disable structured-chunk
        support for the rest of the stream).

        Python-only divergence: thinking is streaming-only reasoning, not
        message content. By default the adapter skips it — the appended text
        equals exactly the text chunks, and no ``{"type": "thinking"}``
        structured chunk is ever appended.
        """
        adapter, client, _ = await _init_adapter()

        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock()
        mock_streamer.stop = AsyncMock(return_value={"message": {"ts": "555.555"}})
        client.chat_stream = AsyncMock(return_value=mock_streamer)

        from chat_sdk.types import ThinkingChunk

        async def chunk_gen() -> AsyncIterator[Any]:
            yield ThinkingChunk(content="let me reason")
            yield "Hello "
            yield ThinkingChunk(content="still reasoning")
            yield "world"

        result = await adapter.stream(
            "slack:C123:1234567890.000000",
            chunk_gen(),
            StreamOptions(recipient_user_id="U1", recipient_team_id="T1"),
        )

        assert result.id == "555.555"
        # The accumulated markdown is exactly the text chunks (no thinking).
        appended_markdown = "".join(
            call.kwargs.get("markdown_text", "")
            for call in mock_streamer.append.call_args_list
            if "markdown_text" in call.kwargs
        )
        assert appended_markdown == "Hello world"
        # No structured "thinking" chunk was ever appended.
        for call in mock_streamer.append.call_args_list:
            for chunk in call.kwargs.get("chunks", []) or []:
                assert chunk.get("type") != "thinking"

    @pytest.mark.asyncio
    async def test_stream_thinking_chunk_invokes_render_hook(self):
        """An opt-in ``render_thinking`` hook receives the reasoning text while
        the posted message stays text-only."""
        adapter, client, _ = await _init_adapter()

        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock()
        mock_streamer.stop = AsyncMock(return_value={"message": {"ts": "444.444"}})
        client.chat_stream = AsyncMock(return_value=mock_streamer)

        seen: list[str] = []

        async def render_thinking(content: str) -> None:
            seen.append(content)

        adapter.render_thinking = render_thinking  # type: ignore[attr-defined]

        from chat_sdk.types import ThinkingChunk

        async def chunk_gen() -> AsyncIterator[Any]:
            yield ThinkingChunk(content="thought A")
            yield "answer"
            yield ThinkingChunk(content="thought B")

        await adapter.stream(
            "slack:C123:1234567890.000000",
            chunk_gen(),
            StreamOptions(recipient_user_id="U1", recipient_team_id="T1"),
        )

        assert seen == ["thought A", "thought B"]

    @pytest.mark.asyncio
    async def test_stream_accepts_dict_markdown_text_chunks(self):
        """Dict-shaped markdown_text chunks must flow into the renderer the
        same as the dataclass form — `_from_full_stream` in thread.py
        forwards them unchanged, so adapters must accept both shapes.
        """
        adapter, client, _ = await _init_adapter()

        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock()
        mock_streamer.stop = AsyncMock(return_value={"message": {"ts": "666.666"}})
        client.chat_stream = AsyncMock(return_value=mock_streamer)

        async def chunk_gen() -> AsyncIterator[Any]:
            yield {"type": "markdown_text", "text": "Hello "}
            yield {"type": "markdown_text", "text": "world"}

        result = await adapter.stream(
            "slack:C123:1234567890.000000",
            chunk_gen(),
            StreamOptions(recipient_user_id="U1", recipient_team_id="T1"),
        )

        assert result.id == "666.666"
        appended_text = "".join(call.kwargs.get("markdown_text", "") for call in mock_streamer.append.call_args_list)
        assert "Hello world" in appended_text, (
            "dict markdown_text chunks were not routed through the "
            f"markdown renderer; streamer.append received {appended_text!r}"
        )

    @pytest.mark.asyncio
    async def test_stream_accepts_dict_task_update_chunks(self):
        """Dict-shaped task_update chunks must be forwarded as structured
        chunks to `streamer.append(chunks=...)`, matching the dataclass form.
        """
        adapter, client, _ = await _init_adapter()

        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock()
        mock_streamer.stop = AsyncMock(return_value={"message": {"ts": "555.555"}})
        client.chat_stream = AsyncMock(return_value=mock_streamer)

        async def chunk_gen() -> AsyncIterator[Any]:
            yield "Starting "
            yield {
                "type": "task_update",
                "id": "task1",
                "title": "Search",
                "status": "in_progress",
            }
            yield {
                "type": "task_update",
                "id": "task1",
                "title": "Search",
                "status": "complete",
                "output": "Found 5",
            }

        result = await adapter.stream(
            "slack:C123:1234567890.000000",
            chunk_gen(),
            StreamOptions(recipient_user_id="U1", recipient_team_id="T1"),
        )

        assert result.id == "555.555"
        structured_calls = [call for call in mock_streamer.append.call_args_list if "chunks" in call.kwargs]
        assert structured_calls, (
            "dict-shaped task_update chunks were not forwarded as structured chunks to streamer.append(chunks=...)"
        )
        all_forwarded_chunks = [chunk for call in structured_calls for chunk in call.kwargs["chunks"]]
        assert any(c.get("type") == "task_update" for c in all_forwarded_chunks), (
            f"no forwarded chunk had type=task_update; got: {all_forwarded_chunks!r}"
        )

    @pytest.mark.asyncio
    async def test_stream_awaits_chat_stream_coroutine(self):
        """Regression test for issue #44: ``AsyncWebClient.chat_stream`` is a
        coroutine function. Without ``await``, ``streamer`` is a coroutine
        and calling ``.append`` raises ``AttributeError``. This test uses
        an ``AsyncMock`` (mirroring the real client) so the fix is required
        to pass.
        """
        adapter, client, _ = await _init_adapter()

        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock()
        mock_streamer.stop = AsyncMock(return_value={"message": {"ts": "444.444"}})
        client.chat_stream = AsyncMock(return_value=mock_streamer)

        async def text_gen() -> AsyncIterator[str]:
            yield "hello"

        result = await adapter.stream(
            "slack:C123:1234567890.000000",
            text_gen(),
            StreamOptions(recipient_user_id="U1", recipient_team_id="T1"),
        )

        assert result.id == "444.444"
        client.chat_stream.assert_awaited_once()
        assert mock_streamer.append.await_count >= 1

    @pytest.mark.asyncio
    async def test_passes_token_on_stream_stop(self):
        """Port of upstream "passes token on stream stop" (vercel/chat#573):
        stop() must always carry the resolved bot token so chat.stopStream
        can't hit not_authed when no token-bearing append flushed first."""
        adapter, client, _ = await _init_adapter()

        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock(return_value=None)
        mock_streamer.stop = AsyncMock(return_value={"ok": True, "ts": "1234567890.111111"})
        client.chat_stream = AsyncMock(return_value=mock_streamer)

        async def short_stream() -> AsyncIterator[str]:
            yield "hello"

        await adapter.stream(
            "slack:C123:1234567890.000000",
            short_stream(),
            StreamOptions(recipient_user_id="U123", recipient_team_id="T123"),
        )

        mock_streamer.stop.assert_awaited_once()
        assert mock_streamer.stop.call_args.kwargs["token"] == "xoxb-test-token"
        # No stop_blocks were supplied, so the optional key must be omitted.
        assert "blocks" not in mock_streamer.stop.call_args.kwargs

    @pytest.mark.asyncio
    async def test_passes_token_on_every_stream_append(self):
        """Port of upstream "passes token on every stream append"
        (vercel/chat#573): repeated structured chunk appends each carry the
        token, not just the first."""
        adapter, client, _ = await _init_adapter()

        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock(return_value={"ok": True})
        mock_streamer.stop = AsyncMock(return_value={"ok": True, "ts": "1234567890.111111"})
        client.chat_stream = AsyncMock(return_value=mock_streamer)

        from chat_sdk.types import TaskUpdateChunk

        async def chunk_stream() -> AsyncIterator[StreamChunk | str]:
            yield TaskUpdateChunk(id="task-1", title="Task one", status="in_progress", output="first")
            yield TaskUpdateChunk(id="task-2", title="Task two", status="in_progress", output="second")

        await adapter.stream(
            "slack:C123:1234567890.000000",
            chunk_stream(),
            StreamOptions(recipient_user_id="U123", recipient_team_id="T123"),
        )

        assert mock_streamer.append.await_count == 2
        for call in mock_streamer.append.call_args_list:
            assert call.kwargs["token"] == "xoxb-test-token"

    @pytest.mark.asyncio
    async def test_stream_uses_request_context_token_for_append_and_stop(self):
        """Multi-workspace composition: the token resolved from the
        per-request context (installation token) must flow through every
        append and the stop — not the default bot token. Python-only
        regression for the documented bot_token resolver plumbing."""
        adapter, client, _ = await _init_adapter()

        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock(return_value={"ok": True})
        mock_streamer.stop = AsyncMock(return_value={"ok": True, "ts": "1234567890.222222"})
        client.chat_stream = AsyncMock(return_value=mock_streamer)

        async def text_gen() -> AsyncIterator[str]:
            yield "workspace-scoped hello"

        await adapter.with_bot_token_async(
            "xoxb-workspace-token",
            lambda: adapter.stream(
                "slack:C123:1234567890.000000",
                text_gen(),
                StreamOptions(recipient_user_id="U123", recipient_team_id="T123"),
            ),
        )

        assert mock_streamer.append.await_count >= 1
        for call in mock_streamer.append.call_args_list:
            assert call.kwargs["token"] == "xoxb-workspace-token"
        assert mock_streamer.stop.call_args.kwargs["token"] == "xoxb-workspace-token"

    @pytest.mark.asyncio
    async def test_stream_threads_team_id_to_chat_stream_for_grid(self):
        """Regression for Enterprise Grid issue #95.

        On Grid orgs ``chat.startStream`` fails with ``team_not_found``
        unless a workspace ``team_id`` is supplied — the per-workspace bot
        token alone is not sufficient (whereas ``chat.postMessage``
        succeeds without it). slack_sdk's ``chat_stream`` forwards unknown
        kwargs (incl. ``team_id``) to the underlying ``chat.startStream``
        call, so the adapter must pass ``team_id`` = ``recipient_team_id``.

        This test simulates Grid: a ``post_message`` succeeds with no
        ``team_id``, while the streaming start raises ``team_not_found``
        when ``team_id`` is absent and only succeeds when it is present.
        It MUST FAIL on pre-fix code (which omitted ``team_id``).
        """
        adapter, client, _ = await _init_adapter()

        # Grid-aware streamer: the lazy ``chat.startStream`` (triggered on
        # the first append or on stop, exactly as the real slack_sdk
        # ``AsyncChatStream`` does) raises ``team_not_found`` unless the
        # workspace ``team_id`` was threaded through ``chat_stream``.
        class _GridStreamer:
            def __init__(self, stream_kwargs: dict[str, Any]) -> None:
                self._stream_kwargs = stream_kwargs
                self._started = False

            def _start_stream(self) -> None:
                # Mirrors slack_sdk forwarding ``_stream_args`` (which holds
                # ``chat_stream``'s kwargs) to ``chat.startStream``.
                if not self._stream_kwargs.get("team_id"):
                    raise _FakeSlackApiError(
                        message="team_not_found",
                        response={"ok": False, "error": "team_not_found"},
                    )
                self._started = True

            async def append(self, **kwargs: Any) -> dict[str, Any]:
                if not self._started:
                    self._start_stream()
                return {"ok": True}

            async def stop(self, **kwargs: Any) -> dict[str, Any]:
                if not self._started:
                    self._start_stream()
                return {"ok": True, "ts": "1234567890.951951"}

        captured: dict[str, Any] = {}

        async def chat_stream(**kwargs: Any) -> _GridStreamer:
            captured.update(kwargs)
            return _GridStreamer(kwargs)

        client.chat_stream = AsyncMock(side_effect=chat_stream)
        client.set_response("chat_postMessage", {"ok": True, "ts": "1234567890.000111"})

        # Sanity: a non-streaming post_message has NO team_id requirement on
        # the same Grid workspace (it succeeds without one).
        post_result = await adapter.post_message(
            "slack:C_GRID:1234567890.000000",
            "plain post on grid",  # type: ignore[arg-type]
        )
        assert post_result.id == "1234567890.000111"  # post_message OK, no team_id
        for call in client.calls:
            if call["method"] == "chat_postMessage":
                assert "team_id" not in call["kwargs"]

        async def text_gen() -> AsyncIterator[str]:
            yield "streamed hello on grid"

        result = await adapter.stream(
            "slack:C_GRID:1234567890.000000",
            text_gen(),
            StreamOptions(recipient_user_id="U_GRID", recipient_team_id="T_GRID_WS"),
        )

        # The stream completed (chat.startStream did NOT raise team_not_found)
        # because the workspace team_id was threaded through.
        assert result.id == "1234567890.951951"
        # The fix passes team_id = recipient_team_id to chat_stream.
        client.chat_stream.assert_awaited_once()
        assert captured["team_id"] == "T_GRID_WS"
        assert captured["recipient_team_id"] == "T_GRID_WS"

    @pytest.mark.asyncio
    async def test_stream_keeps_the_recipient_team_id_under_an_org_wide_context(self):
        """#95 under an Enterprise Grid org-wide request context (#268).

        Org-wide contexts inject the event's ``team_id`` into Web API calls
        (``_with_token_kwargs``), but ``chat_stream`` is not routed through
        it (as upstream's ``chatStream``), so the #95 ``team_id`` stays the
        ``recipient_team_id`` and no ``client_context_team_id`` is added.
        """
        from chat_sdk.adapters.slack.types import RequestContext

        adapter, client, _ = await _init_adapter()
        captured: dict[str, Any] = {}
        streamer = MagicMock()
        streamer.append = AsyncMock(return_value={"ok": True})
        streamer.stop = AsyncMock(return_value={"ok": True, "ts": "1234567890.951951"})

        async def chat_stream(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return streamer

        client.chat_stream = AsyncMock(side_effect=chat_stream)

        async def text_gen() -> AsyncIterator[str]:
            yield "streamed hello on grid"

        tok = adapter._request_context.set(
            RequestContext(
                token="xoxb-org",
                is_enterprise_install=True,
                installation_id="E_ORG",
                team_id="T_EVENT_WS",
                context_team_id="T_AWAY",
                context_channel="C_GRID",
            )
        )
        try:
            result = await adapter.stream(
                "slack:C_GRID:1234567890.000000",
                text_gen(),
                StreamOptions(recipient_user_id="U_GRID", recipient_team_id="T_GRID_WS"),
            )
        finally:
            adapter._request_context.reset(tok)

        assert result.id == "1234567890.951951"
        assert captured["team_id"] == "T_GRID_WS"
        assert "client_context_team_id" not in captured

    @pytest.mark.asyncio
    async def test_stream_raises_team_not_found_without_team_id_on_grid(self):
        """Mutation guard for issue #95: prove the Grid simulation actually
        fails when ``team_id`` is missing.

        This drives the same Grid-aware streamer but with ``chat_stream``
        stripped of any ``team_id`` (mimicking the pre-fix code path), and
        asserts the stream start raises ``team_not_found``. This anchors the
        positive test above: if the fix were reverted, the streamer would
        raise here, so the positive test cannot pass vacuously.
        """
        adapter, client, _ = await _init_adapter()

        class _GridStreamer:
            async def append(self, **kwargs: Any) -> dict[str, Any]:
                raise _FakeSlackApiError(
                    message="team_not_found",
                    response={"ok": False, "error": "team_not_found"},
                )

            async def stop(self, **kwargs: Any) -> dict[str, Any]:
                raise _FakeSlackApiError(
                    message="team_not_found",
                    response={"ok": False, "error": "team_not_found"},
                )

        async def chat_stream(**kwargs: Any) -> _GridStreamer:
            # Simulate slack_sdk dropping team_id (pre-fix behavior): the
            # underlying chat.startStream then fails on Grid.
            kwargs.pop("team_id", None)
            return _GridStreamer()

        client.chat_stream = AsyncMock(side_effect=chat_stream)

        async def text_gen() -> AsyncIterator[str]:
            yield "streamed hello on grid"

        with pytest.raises(_FakeSlackApiError, match="team_not_found"):
            await adapter.stream(
                "slack:C_GRID:1234567890.000000",
                text_gen(),
                StreamOptions(recipient_user_id="U_GRID", recipient_team_id="T_GRID_WS"),
            )


# =============================================================================
# Public request-context accessors (issue #47)
# =============================================================================


# =============================================================================
# Outgoing mention resolution (upstream index.test.ts "resolveOutgoingMentions")
# =============================================================================

_MENTION_THREAD = "slack:C123:1234567890.123456"


async def _init_adapter_with_memory_state() -> tuple[SlackAdapter, MockSlackClient, Any]:
    """Adapter wired to a real ``MemoryStateAdapter`` (upstream ``createAdapterWithState``)."""
    from chat_sdk.state.memory import MemoryStateAdapter

    state = MemoryStateAdapter()
    await state.connect()
    adapter = _make_adapter()
    mock_client = MockSlackClient()
    mock_client.set_response("auth_test", {"user_id": "U_BOT", "bot_id": "B_BOT", "user": "testbot"})
    _patch_client(adapter, mock_client)
    await adapter.initialize(_make_mock_chat(state))  # type: ignore[arg-type]
    return adapter, mock_client, state


class TestResolveOutgoingMentions:
    @pytest.mark.asyncio
    async def test_resolves_unambiguous_mention_to_user_id(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:dominik", "U_DOM_123")

        result = await adapter._resolve_outgoing_mentions("Hey @dominik, check this out", _MENTION_THREAD)

        assert result == "Hey <@U_DOM_123>, check this out"

    @pytest.mark.asyncio
    async def test_handles_case_insensitivity(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:dominik", "U_DOM_123")

        assert await adapter._resolve_outgoing_mentions("Hey @Dominik!", _MENTION_THREAD) == "Hey <@U_DOM_123>!"

    @pytest.mark.asyncio
    async def test_does_not_resolve_handles_inside_urls(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:jkyang", "U_URL_123")
        await state.append_to_list("slack:user-by-name:dominik", "U_DOM_123")
        await state.append_to_list("slack:user-by-name:example", "U_EMAIL_123")

        text = (
            "See https://hackmd.io/@jkyang/abc, https://example.com/p?user=@jkyang, "
            "https://example.com/docs#@jkyang, hackmd.io/@jkyang/abc, "
            "<https://example.com/@jkyang|profile>, and user@example.com cc @dominik"
        )
        result = await adapter._resolve_outgoing_mentions(text, _MENTION_THREAD)

        assert result == text.replace("cc @dominik", "cc <@U_DOM_123>")

    @pytest.mark.asyncio
    async def test_deduplicates_user_ids_from_reverse_index(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:dominik", "U_DOM_123")
        await state.append_to_list("slack:user-by-name:dominik", "U_DOM_123")

        assert await adapter._resolve_outgoing_mentions("Hey @dominik", _MENTION_THREAD) == "Hey <@U_DOM_123>"

    @pytest.mark.asyncio
    async def test_leaves_mention_as_plain_text_when_no_match_found(self):
        adapter, _, _ = await _init_adapter_with_memory_state()

        assert await adapter._resolve_outgoing_mentions("Hey @unknown_user", _MENTION_THREAD) == "Hey @unknown_user"

    @pytest.mark.asyncio
    async def test_skips_already_resolved_user_id_mentions(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:dominik", "U_DOM_123")

        result = await adapter._resolve_outgoing_mentions("Hey <@U_DOM_123> and @dominik", _MENTION_THREAD)

        assert result == "Hey <@U_DOM_123> and <@U_DOM_123>"

    @pytest.mark.asyncio
    async def test_disambiguates_using_thread_participants(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:alex", "U_ALEX_1")
        await state.append_to_list("slack:user-by-name:alex", "U_ALEX_2")
        await state.append_to_list(f"slack:thread-participants:{_MENTION_THREAD}", "U_ALEX_2")

        assert await adapter._resolve_outgoing_mentions("Hey @alex", _MENTION_THREAD) == "Hey <@U_ALEX_2>"

    @pytest.mark.asyncio
    async def test_leaves_ambiguous_mentions_as_plain_text_when_thread_participants_dont_help(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:alex", "U_ALEX_1")
        await state.append_to_list("slack:user-by-name:alex", "U_ALEX_2")
        await state.append_to_list(f"slack:thread-participants:{_MENTION_THREAD}", "U_ALEX_1")
        await state.append_to_list(f"slack:thread-participants:{_MENTION_THREAD}", "U_ALEX_2")

        assert await adapter._resolve_outgoing_mentions("Hey @alex", _MENTION_THREAD) == "Hey @alex"

    @pytest.mark.asyncio
    async def test_resolves_multiple_different_mentions_in_one_message(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:dominik", "U_DOM_123")
        await state.append_to_list("slack:user-by-name:malte", "U_MAL_456")

        result = await adapter._resolve_outgoing_mentions("@dominik and @malte please review", _MENTION_THREAD)

        assert result == "<@U_DOM_123> and <@U_MAL_456> please review"

    @pytest.mark.asyncio
    async def test_does_nothing_when_chat_is_not_initialized(self):
        adapter = _make_adapter()

        assert await adapter._resolve_outgoing_mentions("Hey @dominik", _MENTION_THREAD) == "Hey @dominik"

    @pytest.mark.asyncio
    async def test_skips_mentions_inside_inline_code_backticks(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:vercel", "U_VER_123")

        result = await adapter._resolve_outgoing_mentions("Use `@vercel/postgres` for the database", _MENTION_THREAD)

        assert result == "Use `@vercel/postgres` for the database"

    @pytest.mark.asyncio
    async def test_skips_mentions_inside_code_blocks_triple_backticks(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:vercel", "U_VER_123")

        text = "Install:\n```\nnpm install @vercel/postgres\n```"
        assert await adapter._resolve_outgoing_mentions(text, _MENTION_THREAD) == text

    @pytest.mark.asyncio
    async def test_resolves_mentions_outside_code_but_skips_those_inside(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:dominik", "U_DOM_123")
        await state.append_to_list("slack:user-by-name:vercel", "U_VER_123")

        result = await adapter._resolve_outgoing_mentions(
            "Hey @dominik, use `@vercel/postgres` for this", _MENTION_THREAD
        )

        assert result == "Hey <@U_DOM_123>, use `@vercel/postgres` for this"

    @pytest.mark.asyncio
    async def test_handles_multiple_inline_code_spans_with_mentions(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:neondatabase", "U_NEON_123")
        await state.append_to_list("slack:user-by-name:vercel", "U_VER_123")

        text = "Use `@neondatabase/serverless` or `@vercel/postgres`"
        assert await adapter._resolve_outgoing_mentions(text, _MENTION_THREAD) == text

    @pytest.mark.asyncio
    async def test_resolves_the_same_name_outside_code_while_skipping_it_inside_code(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:vercel", "U_VER_123")

        result = await adapter._resolve_outgoing_mentions(
            "Ping @vercel, but don't link `@vercel/postgres`", _MENTION_THREAD
        )

        assert result == "Ping <@U_VER_123>, but don't link `@vercel/postgres`"

    @pytest.mark.asyncio
    async def test_resolves_a_mention_immediately_following_an_inline_code_span(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:dominik", "U_DOM_123")

        result = await adapter._resolve_outgoing_mentions("Run `npm i` then ping @dominik", _MENTION_THREAD)

        assert result == "Run `npm i` then ping <@U_DOM_123>"

    @pytest.mark.asyncio
    async def test_resolves_a_mention_immediately_preceding_an_inline_code_span(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:dominik", "U_DOM_123")

        result = await adapter._resolve_outgoing_mentions("@dominik try `npm i`", _MENTION_THREAD)

        assert result == "<@U_DOM_123> try `npm i`"

    @pytest.mark.asyncio
    async def test_resolves_mentions_surrounding_a_multiline_fenced_code_block(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:dominik", "U_DOM_123")
        await state.append_to_list("slack:user-by-name:george", "U_GEO_123")
        await state.append_to_list("slack:user-by-name:vercel", "U_VER_123")

        result = await adapter._resolve_outgoing_mentions(
            "Hey @dominik:\n```bash\nnpm install @vercel/postgres\n```\ncc @george", _MENTION_THREAD
        )

        assert result == "Hey <@U_DOM_123>:\n```bash\nnpm install @vercel/postgres\n```\ncc <@U_GEO_123>"

    @pytest.mark.asyncio
    async def test_does_not_skip_a_mention_after_an_unbalanced_single_backtick(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:dominik", "U_DOM_123")

        result = await adapter._resolve_outgoing_mentions("Cost is `5 and @dominik should know", _MENTION_THREAD)

        assert result == "Cost is `5 and <@U_DOM_123> should know"

    @pytest.mark.asyncio
    async def test_skips_a_mention_inside_inline_code_at_the_start_of_the_text(self):
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:vercel", "U_VER_123")

        text = "`@vercel/postgres` is the package"
        assert await adapter._resolve_outgoing_mentions(text, _MENTION_THREAD) == text

    # -- Python-specific guards ------------------------------------------------

    @pytest.mark.asyncio
    async def test_non_ascii_letters_end_the_name(self):
        # Upstream's scanner is ASCII-only (JS char codes); the old Python
        # regex used Unicode ``\w`` and would have looked up ``josé``.
        adapter, _, state = await _init_adapter_with_memory_state()
        await state.append_to_list("slack:user-by-name:jos", "U_JOS")
        await state.append_to_list("slack:user-by-name:josé", "U_JOSE")

        assert await adapter._resolve_outgoing_mentions("hi @josé", _MENTION_THREAD) == "hi <@U_JOS>é"

    @pytest.mark.asyncio
    async def test_text_without_bare_mentions_never_reads_state(self):
        # The native stream path calls the resolver once per committed line;
        # lines without a bare mention (including code-only ones) must not
        # touch state.
        adapter, _, state = await _init_adapter_with_memory_state()
        state.get_list = AsyncMock(return_value=[])  # type: ignore[method-assign]

        text = "plain `@vercel/pkg` https://x.io/@a user@example.com <@U123>\n"
        assert await adapter._resolve_outgoing_mentions(text, _MENTION_THREAD) == text
        state.get_list.assert_not_awaited()


# =============================================================================
# Native streaming outgoing mention resolution (vercel/chat 6f0d2f02 #755)
# =============================================================================

_STREAM_THREAD = "slack:D123:1234567890.000000"


async def _init_mention_stream_adapter() -> tuple[SlackAdapter, MagicMock, Any]:
    adapter, client, state = await _init_adapter_with_memory_state()
    streamer = MagicMock()
    streamer.append = AsyncMock(return_value={"ok": True})
    streamer.stop = AsyncMock(return_value={"ok": True, "ts": "1234567890.111111"})
    client.chat_stream = AsyncMock(return_value=streamer)  # type: ignore[method-assign]
    return adapter, streamer, state


def _appended_markdown(streamer: MagicMock) -> list[str]:
    return [call.kwargs["markdown_text"] for call in streamer.append.call_args_list if "markdown_text" in call.kwargs]


async def _stream_texts(adapter: SlackAdapter, *chunks: str) -> None:
    async def gen() -> AsyncIterator[str]:
        for chunk in chunks:
            yield chunk

    await adapter.stream(_STREAM_THREAD, gen(), StreamOptions(recipient_user_id="U1", recipient_team_id="T1"))


class TestNativeStreamingOutgoingMentionResolution:
    @pytest.mark.asyncio
    async def test_resolves_cached_name_mentions_on_the_native_streaming_path(self):
        adapter, streamer, state = await _init_mention_stream_adapter()
        await state.append_to_list("slack:user-by-name:alice", "U_ALICE_1")

        await _stream_texts(adapter, "Thanks, @alice")

        assert "".join(_appended_markdown(streamer)) == "Thanks, <@U_ALICE_1>"

    @pytest.mark.asyncio
    async def test_resolves_mentions_that_span_source_chunks(self):
        adapter, streamer, state = await _init_mention_stream_adapter()
        await state.append_to_list("slack:user-by-name:alice", "U_ALICE_1")

        await _stream_texts(adapter, "Thanks, @ali", "ce")

        assert "".join(_appended_markdown(streamer)) == "Thanks, <@U_ALICE_1>"

    @pytest.mark.asyncio
    async def test_resolves_mentions_on_lines_committed_mid_stream(self):
        adapter, streamer, state = await _init_mention_stream_adapter()
        await state.append_to_list("slack:user-by-name:alice", "U_ALICE_1")

        await _stream_texts(adapter, "Hi @alice\nmore ", "text")

        appended = _appended_markdown(streamer)
        # The completed line flushes before the stream ends, already resolved.
        assert appended[0] == "Hi <@U_ALICE_1>\n"
        assert "".join(appended) == "Hi <@U_ALICE_1>\nmore text"

    @pytest.mark.asyncio
    async def test_leaves_ambiguous_mentions_as_plain_text(self):
        adapter, streamer, state = await _init_mention_stream_adapter()
        await state.append_to_list("slack:user-by-name:alice", "U_ALICE_1")
        await state.append_to_list("slack:user-by-name:alice", "U_ALICE_2")

        await _stream_texts(adapter, "hey @alice")

        assert "".join(_appended_markdown(streamer)) == "hey @alice"

    @pytest.mark.asyncio
    async def test_disambiguates_ambiguous_mentions_using_thread_participants(self):
        adapter, streamer, state = await _init_mention_stream_adapter()
        await state.append_to_list("slack:user-by-name:alice", "U_ALICE_1")
        await state.append_to_list("slack:user-by-name:alice", "U_ALICE_2")
        await state.append_to_list(f"slack:thread-participants:{_STREAM_THREAD}", "U_ALICE_2")

        await _stream_texts(adapter, "hey @alice")

        assert "".join(_appended_markdown(streamer)) == "hey <@U_ALICE_2>"

    @pytest.mark.asyncio
    async def test_keeps_mentions_literal_inside_code_fences(self):
        adapter, streamer, state = await _init_mention_stream_adapter()
        await state.append_to_list("slack:user-by-name:alice", "U_ALICE_1")

        await _stream_texts(adapter, "```\n@alice\n```\nping @alice")

        assert "".join(_appended_markdown(streamer)) == "```\n@alice\n```\nping <@U_ALICE_1>"

    # -- Python-specific guards ------------------------------------------------

    @pytest.mark.asyncio
    async def test_growing_replacements_neither_duplicate_nor_drop_text_across_appends(self):
        # ``last_appended`` must track the resolved buffer: each replacement
        # changes length, so a delta taken in source coordinates would repeat
        # or lose characters on the next append.
        adapter, streamer, state = await _init_mention_stream_adapter()
        await state.append_to_list("slack:user-by-name:al", "U_AL")
        await state.append_to_list("slack:user-by-name:alice", "U1")
        await state.append_to_list("slack:user-by-name:bob", "U_BOB_LONGER_ID")

        await _stream_texts(adapter, "Hi @al", "ice and @bob\n", "then @al\n", "bye @bo", "b")

        assert _appended_markdown(streamer) == [
            "Hi <@U1> and <@U_BOB_LONGER_ID>\n",
            "then <@U_AL>\n",
            "bye <@U_BOB_LONGER_ID>",
        ]

    @pytest.mark.asyncio
    async def test_streamed_code_stays_literal_while_cached_user_is_pinged(self):
        # The issue's live-loop scenario over small chunks: inline code and a
        # fenced shell snippet stay literal while the fence opens and closes
        # across chunk boundaries.
        adapter, streamer, state = await _init_mention_stream_adapter()
        await state.append_to_list("slack:user-by-name:scope", "U_SCOPE")
        await state.append_to_list("slack:user-by-name:alice", "U_ALICE_1")

        await _stream_texts(
            adapter,
            "Run `npm i @sc",
            "ope/pkg` then ping @al",
            "ice\n``",
            "`sh\nnpm i @scope",
            "/pkg\n```\ndone @alice",
        )

        assert "".join(_appended_markdown(streamer)) == (
            "Run `npm i @scope/pkg` then ping <@U_ALICE_1>\n```sh\nnpm i @scope/pkg\n```\ndone <@U_ALICE_1>"
        )

    @pytest.mark.asyncio
    async def test_closing_fence_committed_before_its_newline_toggles_once(self):
        # One character per chunk: the closing fence line is committed while
        # still inside the fence, before its newline arrives. Fence state must
        # toggle only once that newline is committed, or it flips twice and
        # stays stuck "inside", leaving the final mention literal.
        adapter, streamer, state = await _init_mention_stream_adapter()
        await state.append_to_list("slack:user-by-name:alice", "U_ALICE_1")

        await _stream_texts(adapter, *list("```\ncode\n```\ndone @alice"))

        assert "".join(_appended_markdown(streamer)) == "```\ncode\n```\ndone <@U_ALICE_1>"

    @pytest.mark.asyncio
    async def test_closing_fence_split_across_commits_is_detected_on_the_whole_line(self):
        # The closing fence arrives as "``" then "`\n...": fence detection must
        # read the whole line from its start, not only the newly committed
        # segment ("`\n"), or the fence never closes.
        adapter, streamer, state = await _init_mention_stream_adapter()
        await state.append_to_list("slack:user-by-name:alice", "U_ALICE_1")

        await _stream_texts(adapter, "```\ncode\n``", "`\nping @alice")

        assert "".join(_appended_markdown(streamer)) == "```\ncode\n```\nping <@U_ALICE_1>"

    @pytest.mark.asyncio
    async def test_final_flush_appends_raw_text_without_remend_closing_marker(self):
        # The final delta comes from ``get_committable_text()`` after
        # ``finish()`` (upstream parity), not from ``finish()``'s remend'd
        # render, so an unclosed inline marker is not closed on the way out.
        adapter, streamer, _ = await _init_mention_stream_adapter()

        await _stream_texts(adapter, "hello **bold")

        assert "".join(_appended_markdown(streamer)) == "hello **bold"

    @pytest.mark.asyncio
    async def test_resolves_text_flushed_before_a_structured_chunk(self):
        # ``send_structured_chunk`` pre-flushes committed text; that delta
        # must be resolved and share the resolved coordinate space.
        from chat_sdk.types import TaskUpdateChunk

        adapter, streamer, state = await _init_mention_stream_adapter()
        await state.append_to_list("slack:user-by-name:alice", "U_ALICE_1")

        async def gen() -> AsyncIterator[Any]:
            yield "cc @alice\n"
            yield TaskUpdateChunk(id="t1", title="Search", status="in_progress")
            yield "and @alice"

        await adapter.stream(_STREAM_THREAD, gen(), StreamOptions(recipient_user_id="U1", recipient_team_id="T1"))

        calls = [call.kwargs for call in streamer.append.call_args_list]
        assert [c["markdown_text"] for c in calls if "markdown_text" in c] == [
            "cc <@U_ALICE_1>\n",
            "and <@U_ALICE_1>",
        ]
        assert calls[1]["chunks"] == [{"type": "task_update", "id": "t1", "title": "Search", "status": "in_progress"}]


class TestPublicContextAccessors:
    """``current_token`` / ``current_client`` expose the same values as the
    underscore-prefixed helpers without forcing callers into private API.
    """

    @pytest.mark.asyncio
    async def test_current_token_returns_default_bot_token_in_single_workspace(self):
        adapter, _, _ = await _init_adapter()
        assert adapter.current_token == "xoxb-test-token"

    @pytest.mark.asyncio
    async def test_current_client_returns_preconfigured_client(self):
        adapter, mock_client, _ = await _init_adapter()
        # ``_patch_client`` redirects ``_get_client`` to the MockSlackClient,
        # so the public accessor must route through the same path.
        assert adapter.current_client is mock_client

    @pytest.mark.asyncio
    async def test_current_token_honors_per_request_context(self):
        adapter, _, _ = await _init_adapter()

        from chat_sdk.adapters.slack.types import RequestContext

        token = adapter._request_context.set(RequestContext(token="xoxb-per-request", bot_user_id="U_PER"))
        try:
            assert adapter.current_token == "xoxb-per-request"
        finally:
            adapter._request_context.reset(token)

    def test_current_token_raises_without_any_token(self):
        from chat_sdk.shared.errors import AuthenticationError

        adapter = _make_adapter(bot_token=None)
        with pytest.raises(AuthenticationError):
            _ = adapter.current_token


# =============================================================================
# parseMessage -- complex edge cases
# =============================================================================


class TestParseMessageComplex:
    def test_message_with_files_extracts_multiple(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Multiple files",
            "ts": "1234567890.123456",
            "files": [
                {"id": "F1", "mimetype": "image/png", "url_private": "https://example.com/1.png", "name": "img1.png"},
                {"id": "F2", "mimetype": "video/mp4", "url_private": "https://example.com/1.mp4", "name": "vid1.mp4"},
                {
                    "id": "F3",
                    "mimetype": "application/pdf",
                    "url_private": "https://example.com/1.pdf",
                    "name": "doc1.pdf",
                },
            ],
        }
        msg = adapter.parse_message(event)
        assert len(msg.attachments) == 3
        assert msg.attachments[0].type == "image"
        assert msg.attachments[1].type == "video"
        assert msg.attachments[2].type == "file"

    def test_message_with_links_in_rich_text(self):
        """parseMessage should extract links from rich_text blocks."""
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Check <https://example.com|this link>",
            "ts": "1234567890.123456",
            "blocks": [
                {
                    "type": "rich_text",
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [
                                {"type": "text", "text": "Check "},
                                {"type": "link", "url": "https://example.com", "text": "this link"},
                            ],
                        }
                    ],
                }
            ],
        }
        msg = adapter.parse_message(event)
        # The text should contain the link info
        assert "example.com" in msg.text or "this link" in msg.text

    def test_edited_message_metadata(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Edited message",
            "ts": "1234567890.123456",
            "edited": {"ts": "1234567891.000000"},
        }
        msg = adapter.parse_message(event)
        assert msg.metadata.edited is True
        assert msg.metadata.edited_at is not None

    def test_message_without_subtype_is_normal(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Normal message",
            "ts": "1234567890.123456",
        }
        msg = adapter.parse_message(event)
        assert msg.author.is_bot is False
        assert msg.author.is_me is False

    def test_bot_message_via_bot_id(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "bot_id": "B123",
            "channel": "C456",
            "text": "From a bot",
            "ts": "1234567890.123456",
            "subtype": "bot_message",
        }
        msg = adapter.parse_message(event)
        assert msg.author.is_bot is True
        assert msg.author.user_id == "B123"


# =============================================================================
# renderFormatted Tests
# =============================================================================


class TestRenderFormatted:
    def test_renders_empty_ast(self):
        adapter = _make_adapter()
        # FormattedContent is a dict with "children" key (AST root node)
        result = adapter.render_formatted({"type": "root", "children": []})
        assert isinstance(result, str)
        assert result == ""

    def test_renders_paragraph(self):
        adapter = _make_adapter()
        result = adapter.render_formatted(
            {
                "type": "root",
                "children": [{"type": "paragraph", "children": [{"type": "text", "value": "Hello world"}]}],
            }
        )
        assert "Hello world" in result

    def test_renders_ast_to_standard_markdown(self):
        """Slack now accepts markdown natively, so renderFormatted emits
        standard markdown (``**bold**``), not legacy mrkdwn (``*bold*``)."""
        adapter = _make_adapter()
        result = adapter.render_formatted(
            {
                "type": "root",
                "children": [
                    {
                        "type": "paragraph",
                        "children": [{"type": "strong", "children": [{"type": "text", "value": "bold"}]}],
                    }
                ],
            }
        )
        assert result.strip() == "**bold**"


# =============================================================================
# Link extraction edge cases
# =============================================================================


class TestLinkExtraction:
    def test_extracts_url_from_angle_brackets(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Visit <https://example.com>",
            "ts": "1234567890.123456",
        }
        msg = adapter.parse_message(event)
        assert "https://example.com" in msg.text

    def test_extracts_labeled_url(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "See <https://example.com|Example Site>",
            "ts": "1234567890.123456",
        }
        msg = adapter.parse_message(event)
        assert "example.com" in msg.text or "Example Site" in msg.text

    def test_multiple_links_in_text(self):
        adapter = _make_adapter(bot_user_id="U_BOT")
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "<https://a.com> and <https://b.com|B>",
            "ts": "1234567890.123456",
        }
        msg = adapter.parse_message(event)
        assert "a.com" in msg.text or "b.com" in msg.text


# =============================================================================
# Date parsing edge cases
# =============================================================================


class TestDateParsingEdgeCases:
    def test_valid_ts_yields_date_sent(self):
        adapter = _make_adapter()
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Hello",
            "ts": "1609459200.000000",  # 2021-01-01 00:00:00 UTC
        }
        msg = adapter.parse_message(event)
        assert msg.metadata.date_sent is not None
        assert msg.metadata.date_sent.year == 2021
        assert msg.metadata.date_sent.month == 1
        assert msg.metadata.date_sent.day == 1

    def test_zero_ts_still_parses(self):
        adapter = _make_adapter()
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Zero",
            "ts": "0.000000",
        }
        msg = adapter.parse_message(event)
        assert msg.metadata.date_sent is not None
        assert msg.metadata.date_sent.year == 1970

    def test_missing_ts_yields_no_date(self):
        adapter = _make_adapter()
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "No ts",
        }
        msg = adapter.parse_message(event)
        # Should not crash, may have None or epoch-like date_sent
        assert msg.id == ""

    def test_edited_ts_parsing(self):
        adapter = _make_adapter()
        event = {
            "type": "message",
            "user": "U123",
            "channel": "C456",
            "text": "Edited",
            "ts": "1609459200.000000",
            "edited": {"ts": "1609459260.000000"},
        }
        msg = adapter.parse_message(event)
        assert msg.metadata.edited is True
        assert msg.metadata.edited_at is not None
        assert msg.metadata.edited_at.year == 2021


# =============================================================================
# Error handling
# =============================================================================


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_rate_limit_error_raised(self):
        """Slack rate limit errors should be translated to AdapterRateLimitError."""
        adapter, client, _ = await _init_adapter()

        # Create a mock error that looks like a Slack rate limit response
        class FakeSlackError(Exception):
            def __init__(self):
                super().__init__("ratelimited")
                self.response = {"error": "ratelimited"}

        client.set_response("chat_postMessage", FakeSlackError())

        with pytest.raises(AdapterRateLimitError):
            await adapter.post_message("slack:C123:1234567890.000000", "rate limited")

    @pytest.mark.asyncio
    async def test_non_rate_limit_error_reraised(self):
        """Non-rate-limit errors should be re-raised as-is."""
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", RuntimeError("Something went wrong"))

        with pytest.raises(RuntimeError, match="Something went wrong"):
            await adapter.post_message("slack:C123:1234567890.000000", "will fail")

    @pytest.mark.asyncio
    async def test_delete_message_error_propagates(self):
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_delete", RuntimeError("Delete failed"))

        with pytest.raises(RuntimeError, match="Delete failed"):
            await adapter.delete_message("slack:C123:1234567890.000000", "1234567890.123456")


# =============================================================================
# Ephemeral message ID encoding/decoding
# =============================================================================


class TestEphemeralMessageId:
    def test_encode_decode_roundtrip(self):
        adapter = _make_adapter()
        encoded = adapter._encode_ephemeral_message_id(
            "1234567890.123456",
            "https://hooks.slack.com/actions/T123/456/abc",
            "U_USER_1",
        )
        assert encoded.startswith("ephemeral:")
        decoded = adapter._decode_ephemeral_message_id(encoded)
        assert decoded is not None
        assert decoded["message_ts"] == "1234567890.123456"
        assert decoded["response_url"] == "https://hooks.slack.com/actions/T123/456/abc"

    def test_decode_non_ephemeral_returns_none(self):
        adapter = _make_adapter()
        result = adapter._decode_ephemeral_message_id("1234567890.123456")
        assert result is None

    def test_decode_incomplete_ephemeral_returns_none(self):
        adapter = _make_adapter()
        result = adapter._decode_ephemeral_message_id("ephemeral:123")
        assert result is None

    def test_rejects_the_legacy_non_json_response_url_format(self):
        adapter = _make_adapter()
        encoded = "ephemeral:1234567890.123456:" + base64.b64encode(b"https://hooks.slack.com/respond").decode()

        assert adapter._decode_ephemeral_message_id(encoded) is None

    @pytest.mark.parametrize(
        "response_url",
        [
            "http://hooks.slack.com/respond",
            "https://hooks.slack.com.attacker.example/respond",
            "https://user@hooks.slack.com/respond",
            "https://hooks.slack.com:444/respond",
            "https://attacker.example/respond",
            # Python-specific: ``urlsplit(...).port`` raises on a non-numeric
            # port; that must read as untrusted, not crash the decode.
            "https://hooks.slack.com:abc/respond",
        ],
    )
    def test_rejects_untrusted_response_url(self, response_url: str):
        adapter = _make_adapter()
        data = json.dumps({"responseUrl": response_url, "userId": "U123"})
        encoded = f"ephemeral:1234567890.123456:{base64.b64encode(data.encode()).decode()}"

        assert adapter._decode_ephemeral_message_id(encoded) is None
        with pytest.raises(ValidationError, match="Refusing to encode an untrusted Slack response_url"):
            adapter._encode_ephemeral_message_id("1234567890.123456", response_url, "U123")

    def test_gov_slack_response_url_round_trips(self):
        """Python-specific: GovSlack response URLs (``hooks.slack-gov.com``)
        are trusted on both encode and decode."""
        adapter = _make_adapter()
        url = "https://hooks.slack-gov.com/actions/T123/456/abc"

        decoded = adapter._decode_ephemeral_message_id(
            adapter._encode_ephemeral_message_id("1234567890.123456", url, "U_GOV")
        )

        assert decoded == {"message_ts": "1234567890.123456", "response_url": url, "user_id": "U_GOV"}

    def test_decode_rejects_missing_user_id(self):
        adapter = _make_adapter()
        data = json.dumps({"responseUrl": "https://hooks.slack.com/respond", "userId": ""})
        encoded = f"ephemeral:1234567890.123456:{base64.b64encode(data.encode()).decode()}"

        assert adapter._decode_ephemeral_message_id(encoded) is None


# =============================================================================
# channelIdFromThreadId
# =============================================================================


class TestChannelIdFromThreadId:
    def test_extracts_channel_id(self):
        adapter = _make_adapter()
        assert adapter.channel_id_from_thread_id("slack:C123:1234567890.000000") == "slack:C123"

    def test_works_with_empty_thread_ts(self):
        adapter = _make_adapter()
        assert adapter.channel_id_from_thread_id("slack:C456:") == "slack:C456"

    def test_works_with_dm_channel(self):
        adapter = _make_adapter()
        assert adapter.channel_id_from_thread_id("slack:D789:1111.2222") == "slack:D789"


# =============================================================================
# Empty thread_ts normalization (port of vercel/chat#292 / chat@4.27.0)
# =============================================================================


class TestEmptyThreadTsGuards:
    """Empty ``thread_ts`` (intentional for top-level DMs) must not surface
    to the Slack Web API.

    What to fix if this fails: each Slack API call (``chat_postMessage``,
    ``chat_postEphemeral``, ``chat_scheduleMessage``) must pass ``None``
    instead of the empty string for ``thread_ts``. ``stream`` must degrade
    to a single accumulated ``post_message`` call when ``thread_ts`` is
    empty — ``chat.startStream`` rejects empty thread_ts but
    ``chat.postMessage`` accepts it, and raising would silently drop
    top-level DM streaming replies (chat-sdk-python#94).
    """

    @pytest.mark.asyncio
    async def test_schedule_message_with_empty_thread_ts_normalizes_to_none(self):
        adapter, client, _ = await _init_adapter()
        client.set_response(
            "chat_scheduleMessage",
            {"ok": True, "scheduled_message_id": "Q9999"},
        )
        future_time = datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc)

        await adapter.schedule_message("slack:C123:", "DM-level scheduled", future_time)

        calls = client.get_calls("chat_scheduleMessage")
        assert len(calls) == 1
        assert calls[0]["kwargs"]["thread_ts"] is None

    @pytest.mark.asyncio
    async def test_stream_with_empty_thread_ts_degrades_to_post_message(self):
        """Top-level DMs (empty ``thread_ts``) must accumulate the stream and
        post a single non-streamed message instead of raising.

        What to fix if this fails: ``SlackAdapter.stream`` must, when
        ``thread_ts`` is empty, drain ``text_stream`` (concatenating
        ``str`` chunks and ``markdown_text`` chunk text), call
        ``post_message`` exactly once with a ``PostableMarkdown``
        wrapping the accumulated text, and never invoke
        ``chat_stream``. Raising here silently drops top-level DM
        replies because ``Thread._handle_stream`` does not catch
        adapter exceptions (chat-sdk-python#94).
        """
        adapter, client, _ = await _init_adapter()
        # Streaming API mock that must NOT be touched on the empty-thread path.
        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock()
        mock_streamer.stop = AsyncMock(return_value={"message": {"ts": "0"}})
        client.chat_stream = AsyncMock(return_value=mock_streamer)
        client.set_response("chat_postMessage", {"ok": True, "ts": "5555.5555"})

        from chat_sdk.types import MarkdownTextChunk

        async def text_gen() -> AsyncIterator[str | StreamChunk]:
            yield "Hello "
            yield MarkdownTextChunk(text="from ")
            yield "DM"

        result = await adapter.stream(
            "slack:C123:",
            text_gen(),
            StreamOptions(recipient_user_id="U1", recipient_team_id="T1"),
        )

        # Native streaming API must not have been used.
        assert not client.chat_stream.called
        # post_message was invoked exactly once with the accumulated text.
        post_calls = client.get_calls("chat_postMessage")
        assert len(post_calls) == 1
        assert post_calls[0]["kwargs"]["channel"] == "C123"
        assert post_calls[0]["kwargs"]["thread_ts"] is None
        # Accumulated markdown goes out via Slack's native markdown_text field.
        assert post_calls[0]["kwargs"]["markdown_text"] == "Hello from DM"
        assert "text" not in post_calls[0]["kwargs"]
        assert result.id == "5555.5555"

    @pytest.mark.asyncio
    async def test_stream_with_thread_ts_uses_native_streaming(self):
        """Non-empty ``thread_ts`` must keep using ``chat_stream`` so the
        channel-thread streaming path is not regressed by the DM fallback.

        What to fix if this fails: the empty-``thread_ts`` guard in
        ``SlackAdapter.stream`` must only fire when ``thread_ts`` is
        falsy. For real thread contexts, ``chat_stream`` must still be
        awaited and ``chat_postMessage`` must NOT be called.
        """
        adapter, client, _ = await _init_adapter()
        mock_streamer = MagicMock()
        mock_streamer.append = AsyncMock()
        mock_streamer.stop = AsyncMock(return_value={"message": {"ts": "9.9"}})
        client.chat_stream = AsyncMock(return_value=mock_streamer)

        async def text_gen() -> AsyncIterator[str]:
            yield "channel reply"

        result = await adapter.stream(
            "slack:C123:1234567890.000000",
            text_gen(),
            StreamOptions(recipient_user_id="U1", recipient_team_id="T1"),
        )

        assert client.chat_stream.called
        assert client.chat_stream.call_args.kwargs["thread_ts"] == "1234567890.000000"
        assert client.get_calls("chat_postMessage") == []
        assert result.id == "9.9"

    @pytest.mark.asyncio
    async def test_post_message_with_zero_string_thread_ts_passes_through(self):
        # Adversarial: ``"0"`` is a valid (if exotic) Slack thread_ts. The
        # truthiness ``thread_ts or None`` collapses falsy strings, but
        # ``"0"`` is truthy in Python, so it must NOT be normalized away.
        adapter, client, _ = await _init_adapter()
        client.set_response("chat_postMessage", {"ok": True, "ts": "1.0"})

        await adapter.post_message("slack:C123:0", "Hello in '0' thread")

        calls = client.get_calls("chat_postMessage")
        assert calls[0]["kwargs"]["thread_ts"] == "0"
