"""Faithful translation of transcripts-wiring.test.ts.

Tests for the Chat-level History API wiring: the constructor guard (user
history requires an identity resolver), the ``chat.history.user`` /
deprecated ``chat.transcripts`` accessors, the field-by-field merge of
``history.user`` over the legacy ``transcripts`` block, and the
identity-resolution dispatch hook that populates ``message.user_key``
before handlers run.

TS file: packages/chat/src/transcripts-wiring.test.ts
"""

from __future__ import annotations

from types import SimpleNamespace
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
from chat_sdk.types import (
    ChatConfig,
    HistoryConfig,
    TranscriptsConfig,
    UserHistoryConfig,
)

USER_HISTORY_NOT_CONFIGURED_RE = r"chat\.history\.user is not configured|chat\.transcripts is not configured"
IDENTITY_REQUIRED_RE = r"identity resolver|requires ChatConfig\.identity"

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
    # TS: "throws at construction when history.user is set without identity"
    def test_throws_at_construction_when_historyuser_is_set_without_identity(self, mock_adapter, mock_state):
        with pytest.raises(ValueError, match=IDENTITY_REQUIRED_RE):
            _make_chat(
                mock_adapter,
                mock_state,
                history=HistoryConfig(user=UserHistoryConfig(max_per_user=200)),
            )

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

    # TS: "chat.history.user getter throws when user history was not configured"
    def test_chathistoryuser_getter_throws_when_user_history_was_not_configured(self, mock_adapter, mock_state):
        chat = _make_chat(mock_adapter, mock_state)

        with pytest.raises(ChatError, match=USER_HISTORY_NOT_CONFIGURED_RE):
            _ = chat.history.user

    # TS: "chat.transcripts getter throws when user history was not configured"
    def test_chattranscripts_getter_throws_when_user_history_was_not_configured(self, mock_adapter, mock_state):
        chat = _make_chat(mock_adapter, mock_state)

        with pytest.raises(ChatError, match=USER_HISTORY_NOT_CONFIGURED_RE):
            _ = chat.transcripts

    # TS: "chat.history.user returns the API instance when configured via history.user"
    def test_chathistoryuser_returns_the_api_instance_when_configured_via_historyuser(self, mock_adapter, mock_state):
        chat = _make_chat(
            mock_adapter,
            mock_state,
            history=HistoryConfig(user=UserHistoryConfig(identity=lambda ctx: "u1")),
        )

        api = chat.history.user
        assert callable(api.append)
        assert chat.transcripts is api

    # TS: "merges legacy maxPerUser=%s under history.user"
    @pytest.mark.parametrize("max_per_user", [50, False])
    async def test_merges_legacy_maxperuser_under_historyuser(self, mock_adapter, mock_state, max_per_user):
        # A config migrating one field at a time: identity moved to
        # history.user, retention/max_per_user still on the legacy
        # transcripts block. Both must apply.
        chat = _make_chat(
            mock_adapter,
            mock_state,
            history=HistoryConfig(user=UserHistoryConfig(identity=lambda ctx: "u1")),
            transcripts=TranscriptsConfig(max_per_user=max_per_user, retention="30d"),
        )
        calls: list[tuple[str, dict, int | None, int | None]] = []

        async def _record(key, value, *, max_length=None, ttl_ms=None):
            calls.append((key, value, max_length, ttl_ms))

        mock_state.append_to_list = _record  # type: ignore[method-assign]

        msg = create_test_message("m1", "hello")
        msg.user_key = "u1"
        await chat.history.user.append(SimpleNamespace(adapter=mock_adapter, id="slack:C123:1.2"), msg)

        thirty_days_ms = 30 * 24 * 60 * 60 * 1000
        assert len(calls) == 1
        key, value, max_length, ttl_ms = calls[0]
        assert "u1" in key
        assert value["userKey"] == "u1"
        # ``False`` must reach the backend as "no cap" (None), never as the
        # bool itself.
        assert max_length == (None if max_per_user is False else max_per_user)
        assert ttl_ms == thirty_days_ms

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


# ---------------------------------------------------------------------------
# Python-specific: config precedence (upstream chat.ts constructor)
# ---------------------------------------------------------------------------


class TestHistoryConfigPrecedence:
    async def test_history_user_identity_wins_over_top_level_identity(self, mock_adapter, mock_state):
        # Upstream: `config.history?.user?.identity ?? config.identity`.
        legacy = AsyncMock(return_value="legacy@example.com")
        preferred = AsyncMock(return_value="preferred@example.com")
        handler = AsyncMock(return_value=None)
        chat = _make_chat(
            mock_adapter,
            mock_state,
            identity=legacy,
            history=HistoryConfig(user=UserHistoryConfig(identity=preferred)),
        )

        message = await _dispatch_subscribed_message(chat, mock_adapter, mock_state, handler)

        assert message.user_key == "preferred@example.com"
        legacy.assert_not_called()

    async def test_history_thread_wins_over_thread_history(self, mock_adapter, mock_state):
        # Upstream: `history?.thread ?? threadHistory ?? messageHistory`.
        chat = _make_chat(
            mock_adapter,
            mock_state,
            history=HistoryConfig(thread={"max_messages": 2}),
            thread_history={"max_messages": 5},
        )
        for i in range(3):
            await chat.history.thread.append(THREAD_ID, create_test_message(f"m{i}", f"msg {i}"))

        stored = await mock_state.get_list(f"msg-history:{THREAD_ID}")
        assert [entry["text"] for entry in stored] == ["msg 1", "msg 2"]
