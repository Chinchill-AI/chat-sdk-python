"""Faithful translation of transcripts-wiring.test.ts.

Tests for the Chat-level Transcripts API wiring: the constructor guard
(``transcripts`` requires ``identity``), the ``chat.transcripts`` accessor,
and the identity-resolution dispatch hook that populates
``message.user_key`` before handlers run.

TS file: packages/chat/src/transcripts-wiring.test.ts
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.chat import Chat
from chat_sdk.errors import ChatError
from chat_sdk.testing import (
    MockAdapter,
    MockLogger,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
)
from chat_sdk.types import ChatConfig, TranscriptsConfig

TRANSCRIPTS_NOT_CONFIGURED_RE = r"chat\.transcripts is not configured"
IDENTITY_REQUIRED_RE = r"requires ChatConfig\.identity"

THREAD_ID = "slack:C123:1234.5678"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chat(
    adapter: MockAdapter,
    state: MockStateAdapter,
    **overrides: object,
) -> Chat:
    return Chat(
        ChatConfig(
            user_name="testbot",
            adapters={"slack": adapter},
            state=state,
            logger=overrides.pop("logger", MockLogger()),
            **overrides,  # type: ignore[arg-type]
        )
    )


async def _dispatch_subscribed_message(
    chat: Chat,
    adapter: MockAdapter,
    state: MockStateAdapter,
    handler: AsyncMock,
):
    """Initialize, register a subscribed handler, and dispatch one message."""
    await chat.webhooks["slack"]("request")
    chat.on_subscribed_message(handler)
    await state.subscribe(THREAD_ID)

    message = create_test_message("msg-1", "hello")
    await chat.handle_incoming_message(adapter, THREAD_ID, message)
    return message


@pytest.fixture
def mock_adapter() -> MockAdapter:
    return create_mock_adapter("slack")


@pytest.fixture
def mock_state() -> MockStateAdapter:
    return create_mock_state()


# ---------------------------------------------------------------------------
# Construction / accessor
# ---------------------------------------------------------------------------


class TestTranscriptsApiWiring:
    # TS: "throws at construction when transcripts is set without identity"
    def test_throws_at_construction_when_transcripts_is_set_without_identity(self, mock_adapter, mock_state):
        with pytest.raises(ValueError, match=IDENTITY_REQUIRED_RE):
            _make_chat(mock_adapter, mock_state, transcripts=TranscriptsConfig())

    # TS: "does not throw when neither transcripts nor identity is set"
    def test_does_not_throw_when_neither_transcripts_nor_identity_is_set(self, mock_adapter, mock_state):
        chat = _make_chat(mock_adapter, mock_state)
        assert isinstance(chat, Chat)

    # TS: "does not throw when identity is set without transcripts"
    def test_does_not_throw_when_identity_is_set_without_transcripts(self, mock_adapter, mock_state):
        chat = _make_chat(mock_adapter, mock_state, identity=lambda ctx: "u1")
        assert isinstance(chat, Chat)

    # TS: "chat.transcripts getter throws when transcripts was not configured"
    def test_chattranscripts_getter_throws_when_transcripts_was_not_configured(self, mock_adapter, mock_state):
        chat = _make_chat(mock_adapter, mock_state)

        with pytest.raises(ChatError, match=TRANSCRIPTS_NOT_CONFIGURED_RE):
            _ = chat.transcripts

    # TS: "chat.transcripts returns the API instance when configured"
    def test_chattranscripts_returns_the_api_instance_when_configured(self, mock_adapter, mock_state):
        chat = _make_chat(
            mock_adapter,
            mock_state,
            identity=lambda ctx: "u1",
            transcripts=TranscriptsConfig(),
        )

        api = chat.transcripts
        assert api is not None
        assert callable(api.append)
        assert callable(api.list)
        assert callable(api.count)
        assert callable(api.delete)


# ---------------------------------------------------------------------------
# Dispatch hook
# ---------------------------------------------------------------------------


class TestDispatchHook:
    # TS: "populates message.userKey from the resolver before handlers run"
    async def test_populates_messageuserkey_from_the_resolver_before_handlers_run(self, mock_adapter, mock_state):
        identity = AsyncMock(return_value="user@example.com")
        handler = AsyncMock(return_value=None)

        chat = _make_chat(
            mock_adapter,
            mock_state,
            identity=identity,
            transcripts=TranscriptsConfig(),
        )
        message = await _dispatch_subscribed_message(chat, mock_adapter, mock_state, handler)

        identity.assert_called_once()
        context = identity.call_args.args[0]
        assert context.adapter == "slack"
        assert context.author is message.author
        assert context.message is message
        handler.assert_called()
        assert message.user_key == "user@example.com"

    # TS: "populates message.userKey from a sync resolver that returns a plain string"
    async def test_populates_messageuserkey_from_a_sync_resolver_that_returns_a_plain_string(
        self, mock_adapter, mock_state
    ):
        # The resolver contract allows plain (non-async) callables; MagicMock
        # is deliberate here — its return value is used directly, without
        # being awaited.
        identity = MagicMock(return_value="sync-user@example.com")
        handler = AsyncMock(return_value=None)

        chat = _make_chat(
            mock_adapter,
            mock_state,
            identity=identity,
            transcripts=TranscriptsConfig(),
        )
        message = await _dispatch_subscribed_message(chat, mock_adapter, mock_state, handler)

        identity.assert_called_once()
        handler.assert_called()
        assert message.user_key == "sync-user@example.com"

    # TS: "leaves userKey undefined when the resolver returns null"
    async def test_leaves_userkey_undefined_when_the_resolver_returns_null(self, mock_adapter, mock_state):
        identity = AsyncMock(return_value=None)
        handler = AsyncMock(return_value=None)

        chat = _make_chat(
            mock_adapter,
            mock_state,
            identity=identity,
            transcripts=TranscriptsConfig(),
        )
        message = await _dispatch_subscribed_message(chat, mock_adapter, mock_state, handler)

        handler.assert_called()
        assert message.user_key is None

    # TS: "treats resolver returning empty string as no userKey"
    async def test_treats_resolver_returning_empty_string_as_no_userkey(self, mock_adapter, mock_state):
        identity = AsyncMock(return_value="")
        handler = AsyncMock(return_value=None)

        chat = _make_chat(
            mock_adapter,
            mock_state,
            identity=identity,
            transcripts=TranscriptsConfig(),
        )
        message = await _dispatch_subscribed_message(chat, mock_adapter, mock_state, handler)

        handler.assert_called()
        assert message.user_key is None

    # TS: "logs and proceeds without userKey when the resolver throws"
    async def test_logs_and_proceeds_without_userkey_when_the_resolver_throws(self, mock_adapter, mock_state):
        identity = AsyncMock(side_effect=Exception("lookup failed"))
        handler = AsyncMock(return_value=None)
        logger = MockLogger()

        chat = _make_chat(
            mock_adapter,
            mock_state,
            logger=logger,
            identity=identity,
            transcripts=TranscriptsConfig(),
        )
        message = await _dispatch_subscribed_message(chat, mock_adapter, mock_state, handler)

        warn_calls = [call for call in logger.warn.calls if "Identity resolver threw" in call[0]]
        assert len(warn_calls) == 1
        warn_context = warn_calls[0][1]
        assert isinstance(warn_context["error"], Exception)
        assert warn_context["adapter"] == "slack"
        assert warn_context["thread_id"] == THREAD_ID
        handler.assert_called()
        assert message.user_key is None

    # TS: "does not call the resolver when no identity is configured"
    async def test_does_not_call_the_resolver_when_no_identity_is_configured(self, mock_adapter, mock_state):
        handler = AsyncMock(return_value=None)
        chat = _make_chat(mock_adapter, mock_state)

        message = await _dispatch_subscribed_message(chat, mock_adapter, mock_state, handler)

        handler.assert_called()
        assert message.user_key is None
