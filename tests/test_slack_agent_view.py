"""Slack Agent messaging experience (``agent_view``) and declarative agent config.

Ports the agent_view / suggested prompts / loading messages / feedback
buttons / env-fallback cases of upstream
``packages/adapter-slack/src/index.test.ts`` (vercel/chat 1721fa01, 0f743c9b,
78021c09) and the Slack case of ``packages/integration-tests/src/webhook.test.ts``
(c21ccbc0). Upstream's ``createSlackAdapter`` maps to ``create_slack_adapter``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest

from chat_sdk.adapters.slack.adapter import SlackAdapter, build_feedback_buttons_block, create_slack_adapter
from chat_sdk.adapters.slack.types import (
    SlackAdapterConfig,
    SlackFeedbackButtonsOptions,
    SlackInstallation,
    SlackSuggestedPromptsContext,
    SlackSuggestedPromptsOptions,
)
from chat_sdk.chat import Chat
from chat_sdk.shared.errors import AuthenticationError
from chat_sdk.state.memory import MemoryStateAdapter
from chat_sdk.testing import MockLogger, create_mock_chat_instance, create_mock_state
from chat_sdk.types import (
    AppContextChannelEntity,
    ChatConfig,
    StreamOptions,
    WebhookOptions,
)

SECRET = "test-signing-secret"
ENV_KEYS = ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET", "SLACK_APP_TOKEN")


@pytest.fixture(autouse=True)
def _clean_slack_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class _FakeRequest:
    def __init__(self, body: str, headers: dict[str, str]):
        self.body = body.encode("utf-8")
        self.headers = headers
        self.url = ""

    async def text(self) -> str:
        return self.body.decode("utf-8")


def _signed(body: str, content_type: str = "application/json") -> _FakeRequest:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return _FakeRequest(
        body,
        {"x-slack-request-timestamp": ts, "x-slack-signature": sig, "content-type": content_type},
    )


def _fake_client() -> MagicMock:
    client = MagicMock()
    client.api_call = AsyncMock(return_value={"ok": True})
    client.assistant_threads_setStatus = AsyncMock(return_value={"ok": True})
    client.users_info = AsyncMock(return_value={"ok": True, "user": {"name": "user", "real_name": "User"}})
    return client


def _adapter(**config: Any) -> tuple[SlackAdapter, MagicMock]:
    defaults: dict[str, Any] = {"signing_secret": SECRET, "bot_user_id": "U_BOT", "logger": MockLogger()}
    defaults.update(config)
    adapter = create_slack_adapter(SlackAdapterConfig(**defaults))
    client = _fake_client()
    adapter._get_client = lambda token=None: client  # type: ignore[method-assign]
    return adapter, client


async def _init(adapter: SlackAdapter, state: Any = None) -> Any:
    chat = create_mock_chat_instance(
        state=state if state is not None else create_mock_state(),
        # A real Chat returns the handler task; upstream's mock returns undefined.
        overrides={"process_message": MagicMock(return_value=None)},
    )
    await adapter.initialize(chat)
    return chat


async def _dispatch(adapter: SlackAdapter, payload: dict[str, Any]) -> dict[str, Any]:
    tasks: list[Any] = []
    response = await adapter.handle_webhook(_signed(json.dumps(payload)), WebhookOptions(wait_until=tasks.append))
    await asyncio.gather(*tasks)
    return response


def _prompt_calls(client: MagicMock) -> list[dict[str, Any]]:
    return [
        c.kwargs["json"]
        for c in client.api_call.await_args_list
        if c.kwargs.get("api_method") == "assistant.threads.setSuggestedPrompts"
    ]


# ---------------------------------------------------------------------------
# Env auth fallback (createSlackAdapter noAuthConfig)
# ---------------------------------------------------------------------------


class TestEnvAuthFallback:
    # TS: "keeps SLACK_BOT_TOKEN env fallback when only non-auth config is passed"
    async def test_keeps_slack_bot_token_env_fallback_when_only_non_auth_config_is_passed(self, monkeypatch):
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "env-signing-secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-env-token")
        adapter = create_slack_adapter(SlackAdapterConfig(agent_view=True))
        client = _fake_client()
        tokens: list[str] = []

        def get_client(token: str | None = None) -> MagicMock:
            tokens.append(adapter._get_token() if token is None else token)
            return client

        adapter._get_client = get_client  # type: ignore[method-assign]

        await adapter.set_suggested_prompts("C1", None, [{"title": "t", "message": "m"}])

        assert tokens == ["xoxb-env-token"]
        client.api_call.assert_awaited_once()

    # TS: "disables SLACK_BOT_TOKEN env fallback when another auth field is passed"
    async def test_disables_slack_bot_token_env_fallback_when_another_auth_field_is_passed(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-env-token")
        adapter = create_slack_adapter(
            SlackAdapterConfig(
                signing_secret="config-secret",
                client_id="client-id",
                client_secret="client-secret",
                logger=MockLogger(),
            )
        )

        # Multi-workspace mode: no bot token resolvable outside a request context.
        with pytest.raises(AuthenticationError):
            await adapter.set_suggested_prompts("C1", None, [])

    # TS: "disables SLACK_BOT_TOKEN env fallback for a signingSecret-only config (multi-workspace)"
    async def test_disables_slack_bot_token_env_fallback_for_a_signingsecret_only_config_multi_workspace(
        self, monkeypatch
    ):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-env-token")
        adapter = create_slack_adapter(SlackAdapterConfig(signing_secret="config-secret", logger=MockLogger()))

        with pytest.raises(AuthenticationError):
            await adapter.set_suggested_prompts("C1", None, [])

    # Python-specific (#214 env-fallback decision): the set is upstream
    # createSlackAdapter's. ``app_token`` no longer disables the fallback, and
    # ``installation_provider`` / ``webhook_verifier`` now do.
    def test_app_token_only_socket_config_reads_slack_bot_token(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-env-token")
        adapter = SlackAdapter(SlackAdapterConfig(mode="socket", app_token="xapp-1-test", logger=MockLogger()))
        assert adapter.current_token == "xoxb-env-token"

    @pytest.mark.parametrize("field", ["installation_provider", "webhook_verifier"])
    def test_installation_provider_or_webhook_verifier_disables_env_fallback(self, monkeypatch, field):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-env-token")
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "env-signing-secret")
        monkeypatch.setenv("SLACK_CLIENT_ID", "env-client-id")
        value: Any = MagicMock() if field == "installation_provider" else (lambda request, body: True)
        adapter = SlackAdapter(SlackAdapterConfig(logger=MockLogger(), **{field: value}))
        assert adapter._default_bot_token_provider is None
        assert adapter._client_id is None


# ---------------------------------------------------------------------------
# app_home_opened / app_context_changed
# ---------------------------------------------------------------------------


def _home_opened(tab: str, context: Any = None, **envelope: Any) -> dict[str, Any]:
    event: dict[str, Any] = {"type": "app_home_opened", "user": "U1", "channel": "D1", "tab": tab, "event_ts": "1.2"}
    if context is not None:
        event["context"] = context
    return {"type": "event_callback", **envelope, "event": event}


class TestAgentViewAppHomeOpened:
    # TS: "dispatches app_home_opened for a non-home tab when agentView is enabled"
    async def test_dispatches_app_home_opened_for_a_non_home_tab_when_agentview_is_enabled(self):
        adapter, _ = _adapter(agent_view=True)
        chat = await _init(adapter)

        response = await _dispatch(adapter, _home_opened("messages"))

        assert response["status"] == 200
        chat.process_app_home_opened.assert_called_once()
        event = chat.process_app_home_opened.call_args.args[0]
        assert (event.channel_id, event.user_id, event.tab, event.entities) == ("D1", "U1", "messages", None)

    # TS: "ignores app_home_opened for a non-home tab by default (assistant_view)"
    async def test_ignores_app_home_opened_for_a_non_home_tab_by_default_assistant_view(self):
        adapter, _ = _adapter()
        chat = await _init(adapter)

        await _dispatch(adapter, _home_opened("messages"))

        chat.process_app_home_opened.assert_not_called()

    # TS: "normalizes folded app_home_opened context into entities"
    async def test_normalizes_folded_app_home_opened_context_into_entities(self):
        adapter, _ = _adapter()
        chat = await _init(adapter)

        context = {"entities": [{"type": "slack#/types/channel_id", "value": "C9"}]}
        await _dispatch(adapter, _home_opened("home", context))

        event = chat.process_app_home_opened.call_args.args[0]
        assert (event.channel_id, event.user_id, event.tab) == ("D1", "U1", "home")
        assert event.entities == [AppContextChannelEntity(channel_id="C9")]

    # Python-specific: upstream's ``event.context ? ...`` is JS truthiness, so
    # an empty ``{}`` context still yields ``entities=[]`` (not ``None``).
    async def test_empty_context_object_yields_empty_entities(self):
        adapter, _ = _adapter()
        chat = await _init(adapter)

        await _dispatch(adapter, _home_opened("home", {}))

        assert chat.process_app_home_opened.call_args.args[0].entities == []


def _context_changed(**event: Any) -> dict[str, Any]:
    return {
        "type": "event_callback",
        "event": {"type": "app_context_changed", "channel": "D1", "user": "U1", "event_ts": "1.2", **event},
    }


class TestAppContextChanged:
    # TS: "routes app_context_changed with normalized entities"
    async def test_routes_app_context_changed_with_normalized_entities(self):
        adapter, _ = _adapter()
        chat = await _init(adapter)
        payload = _context_changed(context={"entities": [{"type": "slack#/types/channel_id", "value": "C9"}]})

        response = await _dispatch(adapter, payload)

        assert response["status"] == 200
        chat.process_app_context_changed.assert_called_once()
        event = chat.process_app_context_changed.call_args.args[0]
        assert (event.channel_id, event.user_id, event.adapter) == ("D1", "U1", adapter)
        assert event.entities == [AppContextChannelEntity(channel_id="C9")]
        assert event.raw == payload["event"]

    # TS: "passes empty entities for an empty context object"
    async def test_passes_empty_entities_for_an_empty_context_object(self):
        adapter, _ = _adapter()
        chat = await _init(adapter)

        await _dispatch(adapter, _context_changed(context={}))

        assert chat.process_app_context_changed.call_args.args[0].entities == []

    # TS: "returns 200 with empty entities when the payload has no context field"
    async def test_returns_200_with_empty_entities_when_the_payload_has_no_context_field(self):
        adapter, _ = _adapter()
        chat = await _init(adapter)

        response = await _dispatch(adapter, _context_changed())

        assert response["status"] == 200
        assert chat.process_app_context_changed.call_args.args[0].entities == []


# ---------------------------------------------------------------------------
# agent_view DM threading + open_dm bridge
# ---------------------------------------------------------------------------


def _dm_message(**extra: Any) -> dict[str, Any]:
    return {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "text": "hi",
            "ts": "1771.99",
            "event_ts": "1771.99",
            **extra,
        },
    }


class TestAgentViewDmThreading:
    # TS: "threads a top-level agent_view DM message under its own ts"
    async def test_threads_a_top_level_agent_view_dm_message_under_its_own_ts(self):
        adapter, _ = _adapter(agent_view=True)
        chat = await _init(adapter)

        await _dispatch(adapter, _dm_message())

        assert chat.process_message.call_args.args[1] == "slack:D1:1771.99"

    # TS: "routes a top-level agent_view DM message to the conversation-scoped
    # thread when it is subscribed (openDM flow)"
    async def test_routes_a_top_level_agent_view_dm_message_to_the_conversation_scoped_thread_when_subscribed(self):
        adapter, _ = _adapter(agent_view=True)
        state = create_mock_state()
        await state.subscribe("slack:D1:")
        chat = await _init(adapter, state)

        await _dispatch(adapter, _dm_message())

        assert chat.process_message.call_args.args[1] == "slack:D1:"
        # The parsed message carries the routed thread ID too (not the
        # per-message ``slack:D1:1771.99``).
        message = await chat.process_message.call_args.args[2]()
        assert message.thread_id == "slack:D1:"

    # TS: "keeps DM top-level messages conversation-scoped without agentView"
    async def test_keeps_dm_top_level_messages_conversation_scoped_without_agentview(self):
        adapter, _ = _adapter()
        chat = await _init(adapter)

        await adapter.handle_webhook(_signed(json.dumps(_dm_message())))

        assert chat.process_message.call_args.args[1] == "slack:D1:"

    # Python-specific: a threaded agent_view DM reply keeps its thread root
    # and skips the bridge (process_message is called synchronously).
    async def test_threaded_agent_view_dm_reply_keeps_thread_root(self):
        adapter, _ = _adapter(agent_view=True)
        state = create_mock_state()
        await state.subscribe("slack:D1:")
        chat = await _init(adapter, state)

        await adapter.handle_webhook(_signed(json.dumps(_dm_message(thread_ts="1771.00"))))

        assert chat.process_message.call_args.args[1] == "slack:D1:1771.00"

    # Python-specific: a failing subscription check warns and keeps the
    # per-message thread ID.
    async def test_subscription_check_failure_keeps_per_message_thread(self):
        logger = MockLogger()
        adapter, _ = _adapter(agent_view=True, logger=logger)
        state = create_mock_state()
        state.is_subscribed = AsyncMock(side_effect=RuntimeError("state down"))  # type: ignore[method-assign]
        chat = await _init(adapter, state)

        await _dispatch(adapter, _dm_message())

        assert chat.process_message.call_args.args[1] == "slack:D1:1771.99"
        assert [c[0] for c in logger.warn.calls] == [
            "agent_view DM subscription check failed; using per-message thread"
        ]

    # Python-specific: a host cancelling its wait_until awaitable (after the
    # webhook returned 200) must not drop the DM; upstream promises cannot be
    # cancelled.
    async def test_cancelling_wait_until_does_not_drop_the_dm(self):
        adapter, _ = _adapter(agent_view=True)
        state = create_mock_state()
        release = asyncio.Event()

        async def slow_is_subscribed(thread_id: str) -> bool:
            await release.wait()
            return False

        state.is_subscribed = slow_is_subscribed  # type: ignore[method-assign]
        chat = await _init(adapter, state)
        tasks: list[Any] = []

        await adapter.handle_webhook(_signed(json.dumps(_dm_message())), WebhookOptions(wait_until=tasks.append))
        await asyncio.sleep(0)
        tasks[0].cancel()
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)

        assert tasks[0].cancelled()
        assert chat.process_message.call_args.args[1] == "slack:D1:1771.99"

    # Python-specific: the bridge task is created inside the request
    # context, so the multi-workspace installation token is visible to it.
    async def test_bridge_task_sees_the_installation_token(self):
        provider = MagicMock()
        provider.get_installation = AsyncMock(return_value=SlackInstallation(bot_token="xoxb-team-token"))
        adapter, _ = _adapter(agent_view=True, bot_user_id=None, installation_provider=provider)
        seen: list[str] = []
        chat = create_mock_chat_instance(
            state=create_mock_state(),
            overrides={"process_message": MagicMock(side_effect=lambda *a: seen.append(adapter.current_token))},
        )
        await adapter.initialize(chat)

        await _dispatch(adapter, {**_dm_message(), "team_id": "T1"})

        assert seen == ["xoxb-team-token"]


# ---------------------------------------------------------------------------
# Bridge error handling (integration-tests/src/webhook.test.ts, c21ccbc0)
# ---------------------------------------------------------------------------


def _real_chat() -> tuple[Chat, MemoryStateAdapter]:
    adapter, _ = _adapter(agent_view=True, bot_token="xoxb-test-token")
    state = MemoryStateAdapter()
    chat = Chat(ChatConfig(user_name="bot", adapters={"slack": adapter}, state=state, logger=MockLogger()))
    return chat, state


class TestAgentViewBridgeErrors:
    # TS: "tracks delayed Slack agent-view handlers when failing=%s"
    # (describe.each "webhook handler errors with propagateHandlerErrors=%s")
    @pytest.mark.parametrize("propagate", [None, False, True])
    @pytest.mark.parametrize("failing", [False, True])
    async def test_tracks_delayed_slack_agent_view_handlers(self, propagate, failing):
        chat, state = _real_chat()
        error = RuntimeError("Database admission failed")
        calls: list[Any] = []

        async def handler(*args: Any) -> None:
            calls.append(args)
            if failing:
                raise error

        chat.on_direct_message(handler)
        release = asyncio.Event()
        original = state.is_subscribed

        async def delayed_is_subscribed(thread_id: str) -> bool:
            state.is_subscribed = original  # type: ignore[method-assign]
            await release.wait()
            return False

        state.is_subscribed = delayed_is_subscribed  # type: ignore[method-assign]
        tasks: list[Any] = []
        try:
            response = await chat.webhooks["slack"](
                _signed(json.dumps(_dm_message())),
                WebhookOptions(wait_until=tasks.append, propagate_handler_errors=propagate),
            )
            assert response["status"] == 200
            await asyncio.sleep(0)
            assert calls == []
            assert len(tasks) == 1

            release.set()
            results = await asyncio.gather(tasks[0], return_exceptions=True)
            await asyncio.gather(*tasks, return_exceptions=True)

            assert len(calls) == 1
            assert len(tasks) == 2
            assert results == [error if failing and propagate else None]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await chat.shutdown()

    # TS: "keeps Slack failures handled without a waitUntil callback"
    async def test_keeps_slack_failures_handled_without_a_waituntil_callback(self):
        chat, _ = _real_chat()
        logger = chat.get_adapter("slack")._logger  # type: ignore[union-attr]
        handler = AsyncMock(side_effect=RuntimeError("Database admission failed"))
        chat.on_direct_message(handler)
        loop = asyncio.get_running_loop()
        unhandled: list[dict[str, Any]] = []
        loop.set_exception_handler(lambda _loop, ctx: unhandled.append(ctx))
        try:
            response = await chat.webhooks["slack"](
                _signed(json.dumps(_dm_message())), WebhookOptions(propagate_handler_errors=True)
            )
            for _ in range(100):
                if handler.await_count:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.01)
            handler.assert_awaited_once()
            assert response["status"] == 200
            # The bridge swallowed the failure: no "exception was never retrieved".
            assert unhandled == []
            # ... and did not re-raise it (only done with a wait_until to observe it).
            assert [c[0] for c in logger.warn.calls] == ["Agent view DM processing failed"]
            assert [c for c in logger.debug.calls if c[0] == "Agent view DM bridge re-raised a handler error"] == []
        finally:
            loop.set_exception_handler(None)
            await chat.shutdown()


# ---------------------------------------------------------------------------
# Configured suggested prompts
# ---------------------------------------------------------------------------


def _assistant_thread_started() -> dict[str, Any]:
    return {
        "type": "event_callback",
        "team_id": "T123",
        "event": {
            "type": "assistant_thread_started",
            "event_ts": "1234567890.000000",
            "assistant_thread": {
                "user_id": "U_USER",
                "channel_id": "D_ASSISTANT",
                "thread_ts": "1234567890.111111",
                "context": {"channel_id": "C_CONTEXT", "team_id": "T123"},
            },
        },
    }


async def _prompts_setup(**config: Any) -> tuple[SlackAdapter, MagicMock, MockLogger]:
    logger = MockLogger()
    adapter, client = _adapter(bot_token="xoxb-test-token", logger=logger, **config)
    await _init(adapter)
    return adapter, client, logger


def _home_opened_t123(tab: str, context: Any = None) -> dict[str, Any]:
    payload = _home_opened(tab, context, team_id="T123")
    payload["event"]["user"] = "U_USER"
    return payload


class TestConfiguredSuggestedPrompts:
    # TS: "applies static prompts on assistant_thread_started (legacy assistant_view)"
    async def test_applies_static_prompts_on_assistant_thread_started_legacy_assistant_view(self):
        adapter, client, _ = await _prompts_setup(
            suggested_prompts=SlackSuggestedPromptsOptions(
                title="Welcome!", prompts=[{"title": "Ideas", "message": "Generate ideas"}]
            )
        )

        response = await _dispatch(adapter, _assistant_thread_started())

        assert response["status"] == 200
        assert _prompt_calls(client) == [
            {
                "channel_id": "D_ASSISTANT",
                "thread_ts": "1234567890.111111",
                "title": "Welcome!",
                "prompts": [{"title": "Ideas", "message": "Generate ideas"}],
            }
        ]

    # TS: "applies prompts on Messages-tab app_home_opened under agentView, without thread_ts"
    async def test_applies_prompts_on_messages_tab_app_home_opened_under_agentview_without_thread_ts(self):
        adapter, client, _ = await _prompts_setup(
            agent_view=True,
            suggested_prompts=SlackSuggestedPromptsOptions(
                prompts=[{"title": "Summarize", "message": "Summarize this channel"}]
            ),
        )

        await _dispatch(adapter, _home_opened_t123("messages"))

        assert _prompt_calls(client) == [
            {"channel_id": "D1", "prompts": [{"title": "Summarize", "message": "Summarize this channel"}]}
        ]

    # TS: "does not apply prompts on a Home-tab open under agentView"
    async def test_does_not_apply_prompts_on_a_home_tab_open_under_agentview(self):
        adapter, client, _ = await _prompts_setup(
            agent_view=True,
            suggested_prompts=SlackSuggestedPromptsOptions(prompts=[{"title": "S", "message": "M"}]),
        )

        await _dispatch(adapter, _home_opened_t123("home"))

        assert _prompt_calls(client) == []

    # TS: "does nothing when suggestedPrompts is not configured"
    async def test_does_nothing_when_suggestedprompts_is_not_configured(self):
        adapter, client, _ = await _prompts_setup()

        await _dispatch(adapter, _assistant_thread_started())

        assert _prompt_calls(client) == []

    # TS: "invokes a dynamic resolver with thread context"
    async def test_invokes_a_dynamic_resolver_with_thread_context(self):
        resolver = AsyncMock(
            return_value=SlackSuggestedPromptsOptions(prompts=[{"title": "Dynamic", "message": "Resolved per thread"}])
        )
        adapter, client, _ = await _prompts_setup(suggested_prompts=resolver)

        await _dispatch(adapter, _assistant_thread_started())

        resolver.assert_awaited_once_with(
            SlackSuggestedPromptsContext(
                channel_id="D_ASSISTANT", user_id="U_USER", team_id="T123", thread_ts="1234567890.111111"
            )
        )
        sent = [c["prompts"] for c in _prompt_calls(client)]
        assert sent == [[{"title": "Dynamic", "message": "Resolved per thread"}]]

    # TS: "passes team and active-view entities to the resolver under agentView"
    async def test_passes_team_and_active_view_entities_to_the_resolver_under_agentview(self):
        resolver = AsyncMock(return_value=None)
        adapter, client, _ = await _prompts_setup(agent_view=True, suggested_prompts=resolver)

        await _dispatch(
            adapter,
            _home_opened_t123(
                "messages", {"entities": [{"type": "slack#/types/channel_id", "value": "C42", "team_id": "T123"}]}
            ),
        )

        resolver.assert_awaited_once_with(
            SlackSuggestedPromptsContext(
                channel_id="D1",
                user_id="U_USER",
                team_id="T123",
                entities=[AppContextChannelEntity(channel_id="C42", team_id="T123")],
            )
        )
        assert _prompt_calls(client) == []

    # Python-specific (vercel/chat#889): ``authorizations[0].team_id`` wins
    # over the envelope ``team_id``.
    async def test_resolver_team_id_prefers_authorizations(self):
        resolver = AsyncMock(return_value=None)
        adapter, _, _ = await _prompts_setup(agent_view=True, suggested_prompts=resolver)
        payload = _home_opened_t123("messages")
        payload["authorizations"] = [{"team_id": "T_AUTH"}]

        await _dispatch(adapter, payload)

        assert resolver.await_args.args[0].team_id == "T_AUTH"

    # Python-specific (upstream normalizes separately, index.ts:4049-4062):
    # a resolver mutating its context's entities must not change what the
    # app_home_opened handlers see.
    async def test_resolver_entities_are_not_shared_with_app_home_opened_event(self):
        def resolver(ctx: SlackSuggestedPromptsContext) -> None:
            assert ctx.entities is not None
            ctx.entities.clear()

        adapter, _, _ = await _prompts_setup(agent_view=True, suggested_prompts=resolver)
        chat = adapter._chat

        await _dispatch(
            adapter, _home_opened_t123("messages", {"entities": [{"type": "slack#/types/channel_id", "value": "C42"}]})
        )

        event = chat.process_app_home_opened.call_args.args[0]  # type: ignore[union-attr]
        assert event.entities == [AppContextChannelEntity(channel_id="C42")]

    # Python-specific: as for the DM bridge, a host cancelling its wait_until
    # awaitable must not cancel the prompts task (upstream promises cannot be
    # cancelled).
    async def test_cancelling_wait_until_does_not_drop_the_prompts(self):
        release = asyncio.Event()

        async def resolver(_ctx: SlackSuggestedPromptsContext) -> SlackSuggestedPromptsOptions:
            await release.wait()
            return SlackSuggestedPromptsOptions(prompts=[{"title": "Late", "message": "Still applied"}])

        adapter, client, _ = await _prompts_setup(suggested_prompts=resolver)
        tasks: list[Any] = []

        await adapter.handle_webhook(
            _signed(json.dumps(_assistant_thread_started())), WebhookOptions(wait_until=tasks.append)
        )
        await asyncio.sleep(0)
        tasks[0].cancel()
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)

        assert tasks[0].cancelled()
        assert [c["prompts"] for c in _prompt_calls(client)] == [[{"title": "Late", "message": "Still applied"}]]

    # TS: "skips setting prompts when the resolver returns null"
    async def test_skips_setting_prompts_when_the_resolver_returns_null(self):
        adapter, client, _ = await _prompts_setup(suggested_prompts=lambda _ctx: None)

        await _dispatch(adapter, _assistant_thread_started())

        assert _prompt_calls(client) == []

    # Python-specific: a sync resolver's return value is used directly.
    async def test_sync_resolver_prompts_are_applied(self):
        seen: list[SlackSuggestedPromptsContext] = []

        def resolver(ctx: SlackSuggestedPromptsContext) -> SlackSuggestedPromptsOptions:
            seen.append(ctx)
            return SlackSuggestedPromptsOptions(prompts=[{"title": "Sync", "message": "From a sync resolver"}])

        adapter, client, _ = await _prompts_setup(suggested_prompts=resolver)

        await _dispatch(adapter, _assistant_thread_started())

        assert [ctx.thread_ts for ctx in seen] == ["1234567890.111111"]
        assert [c["prompts"] for c in _prompt_calls(client)] == [[{"title": "Sync", "message": "From a sync resolver"}]]

    # TS: "truncates to Slack's 4-prompt limit with a warning"
    async def test_truncates_to_slacks_4_prompt_limit_with_a_warning(self):
        prompts: list[Any] = [{"title": f"P{i}", "message": f"M{i}"} for i in range(6)]
        adapter, client, logger = await _prompts_setup(suggested_prompts=SlackSuggestedPromptsOptions(prompts=prompts))

        await _dispatch(adapter, _assistant_thread_started())

        sent = _prompt_calls(client)[0]["prompts"]
        assert len(sent) == 4
        assert sent[3] == {"title": "P3", "message": "M3"}
        assert logger.warn.calls == [
            ("Slack shows at most 4 suggested prompts; dropping the rest", {"configured": 6}),
        ]

    # TS: "logs and keeps the webhook green when the resolver throws"
    async def test_logs_and_keeps_the_webhook_green_when_the_resolver_throws(self):
        def resolver(_ctx: SlackSuggestedPromptsContext) -> SlackSuggestedPromptsOptions:
            raise RuntimeError("resolver blew up")

        adapter, client, logger = await _prompts_setup(suggested_prompts=resolver)

        response = await _dispatch(adapter, _assistant_thread_started())

        assert response["status"] == 200
        assert _prompt_calls(client) == []
        assert [c[0] for c in logger.warn.calls] == ["Failed to apply configured suggested prompts"]

    # TS: "logs and keeps the webhook green when the API call fails"
    async def test_logs_and_keeps_the_webhook_green_when_the_api_call_fails(self):
        adapter, client, logger = await _prompts_setup(
            suggested_prompts=SlackSuggestedPromptsOptions(prompts=[{"title": "Ideas", "message": "Generate ideas"}])
        )
        client.api_call.side_effect = RuntimeError("slack down")

        response = await _dispatch(adapter, _assistant_thread_started())

        assert response["status"] == 200
        assert len(_prompt_calls(client)) == 1
        assert [c[0] for c in logger.warn.calls] == ["Failed to apply configured suggested prompts"]


# ---------------------------------------------------------------------------
# Configured loading messages
# ---------------------------------------------------------------------------


def _status_adapter(loading_messages: list[str] | None = None) -> tuple[SlackAdapter, MagicMock]:
    return _adapter(bot_token="xoxb-test-token", loading_messages=loading_messages)


class TestConfiguredLoadingMessages:
    # TS: "startTyping uses configured loading messages by default"
    async def test_starttyping_uses_configured_loading_messages_by_default(self):
        adapter, client = _status_adapter(["Thinking...", "Digging..."])

        await adapter.start_typing("slack:D1:1.2")

        client.assistant_threads_setStatus.assert_awaited_once_with(
            channel_id="D1", thread_ts="1.2", status="Thinking...", loading_messages=["Thinking...", "Digging..."]
        )

    # TS: "startTyping prefers an explicit status over configured messages"
    async def test_starttyping_prefers_an_explicit_status_over_configured_messages(self):
        adapter, client = _status_adapter(["Thinking..."])

        await adapter.start_typing("slack:D1:1.2", "Searching docs...")

        client.assistant_threads_setStatus.assert_awaited_once_with(
            channel_id="D1", thread_ts="1.2", status="Searching docs...", loading_messages=["Searching docs..."]
        )

    # TS: "setAssistantStatus falls back to configured loading messages"
    async def test_setassistantstatus_falls_back_to_configured_loading_messages(self):
        adapter, client = _status_adapter(["Thinking..."])

        await adapter.set_assistant_status("D1", "1.2", "working")

        client.assistant_threads_setStatus.assert_awaited_once_with(
            channel_id="D1", thread_ts="1.2", status="working", loading_messages=["Thinking..."]
        )

    # TS: "setAssistantStatus explicit loadingMessages win over config"
    async def test_setassistantstatus_explicit_loadingmessages_win_over_config(self):
        adapter, client = _status_adapter(["Thinking..."])

        await adapter.set_assistant_status("D1", "1.2", "working", ["Custom..."])

        assert client.assistant_threads_setStatus.await_args.kwargs["loading_messages"] == ["Custom..."]

    # TS: "startTyping keeps the Typing... default without config"
    async def test_starttyping_keeps_the_typing_default_without_config(self):
        adapter, client = _status_adapter()

        await adapter.start_typing("slack:D1:1.2")

        client.assistant_threads_setStatus.assert_awaited_once_with(
            channel_id="D1", thread_ts="1.2", status="Typing...", loading_messages=["Typing..."]
        )

    # Python-specific (upstream ``loadingMessages ?? this.loadingMessages``):
    # an explicit ``[]`` is not replaced by the config; empty lists are
    # omitted from the request.
    async def test_setassistantstatus_explicit_empty_loading_messages_skip_config(self):
        adapter, client = _status_adapter(["Thinking..."])

        await adapter.set_assistant_status("D1", "1.2", "working", [])

        client.assistant_threads_setStatus.assert_awaited_once_with(channel_id="D1", thread_ts="1.2", status="working")

    # Python-specific (upstream ``this.loadingMessages ?? ["Typing..."]``): a
    # configured ``[]`` is sent as-is; the status still defaults to "Typing...".
    async def test_starttyping_sends_configured_empty_loading_messages(self):
        adapter, client = _status_adapter([])

        await adapter.start_typing("slack:D1:1.2")

        client.assistant_threads_setStatus.assert_awaited_once_with(
            channel_id="D1", thread_ts="1.2", status="Typing...", loading_messages=[]
        )

    # Python-specific (upstream ``??`` parity): an explicit empty status is
    # sent as-is (clearing the indicator) instead of becoming "Typing...".
    async def test_starttyping_sends_explicit_empty_status(self):
        adapter, client = _status_adapter(["Thinking..."])

        await adapter.start_typing("slack:D1:1.2", "")

        client.assistant_threads_setStatus.assert_awaited_once_with(
            channel_id="D1", thread_ts="1.2", status="", loading_messages=["Thinking..."]
        )


# ---------------------------------------------------------------------------
# Feedback buttons
# ---------------------------------------------------------------------------


def _stream_adapter(feedback_buttons: Any = None) -> tuple[SlackAdapter, MagicMock]:
    adapter, client = _adapter(bot_token="xoxb-test-token", feedback_buttons=feedback_buttons)
    streamer = MagicMock()
    streamer.append = AsyncMock(return_value={"ok": True})
    streamer.stop = AsyncMock(return_value={"ok": True, "ts": "1234567890.111111"})
    client.chat_stream = AsyncMock(return_value=streamer)
    return adapter, streamer


async def _hello() -> AsyncIterator[str]:
    yield "hello"


async def _stream(adapter: SlackAdapter, **options: Any) -> None:
    await adapter.stream(
        "slack:D123:1234567890.000000",
        _hello(),
        StreamOptions(recipient_user_id="U1", recipient_team_id="T1", **options),
    )


class TestFeedbackButtons:
    # TS: "appends a feedback context_actions block on stream stop"
    async def test_appends_a_feedback_context_actions_block_on_stream_stop(self):
        adapter, streamer = _stream_adapter(True)

        await _stream(adapter)

        assert streamer.stop.await_args.kwargs["blocks"] == [build_feedback_buttons_block()]

    # TS: "honors custom labels, values, and action id"
    async def test_honors_custom_labels_values_and_action_id(self):
        adapter, streamer = _stream_adapter(
            SlackFeedbackButtonsOptions(
                action_id="ai_feedback",
                negative_label="Nope",
                negative_value="down",
                positive_label="Nice",
                positive_value="up",
            )
        )

        await _stream(adapter)

        element = streamer.stop.await_args.kwargs["blocks"][0]["elements"][0]
        assert element["action_id"] == "ai_feedback"
        assert element["positive_button"] == {"text": {"type": "plain_text", "text": "Nice"}, "value": "up"}
        assert element["negative_button"] == {"text": {"type": "plain_text", "text": "Nope"}, "value": "down"}

    # TS: "places feedback buttons after caller stopBlocks"
    async def test_places_feedback_buttons_after_caller_stopblocks(self):
        adapter, streamer = _stream_adapter(True)
        end_with = {"type": "actions", "elements": []}

        await _stream(adapter, stop_blocks=[end_with])

        blocks = streamer.stop.await_args.kwargs["blocks"]
        assert len(blocks) == 2
        assert blocks[0] == end_with
        assert blocks[1]["type"] == "context_actions"

    # TS: "attaches no blocks when unconfigured"
    @pytest.mark.parametrize("feedback_buttons", [None, False])
    async def test_attaches_no_blocks_when_unconfigured(self, feedback_buttons):
        adapter, streamer = _stream_adapter(feedback_buttons)

        await _stream(adapter)

        assert "blocks" not in streamer.stop.await_args.kwargs

    # TS: "routes feedback button clicks through onAction"
    async def test_routes_feedback_button_clicks_through_onaction(self):
        adapter, _ = _adapter(bot_token="xoxb-test-token")
        chat = await _init(adapter)
        payload = {
            "type": "block_actions",
            "user": {"id": "U1", "username": "user"},
            "trigger_id": "trigger-1",
            "channel": {"id": "D123", "name": "dm"},
            "container": {"type": "message", "message_ts": "1234567890.111111", "channel_id": "D123"},
            "message": {"ts": "1234567890.111111", "thread_ts": "1234567890.000000"},
            "actions": [
                {
                    "type": "feedback_buttons",
                    "action_id": "message_feedback",
                    "value": "positive",
                    "action_ts": "1234567891.000000",
                }
            ],
        }
        body = f"payload={quote(json.dumps(payload))}"

        response = await adapter.handle_webhook(_signed(body, "application/x-www-form-urlencoded"))

        assert response["status"] == 200
        chat.process_action.assert_called_once()
        event = chat.process_action.call_args.args[0]
        assert (event.action_id, event.value, event.thread_id) == (
            "message_feedback",
            "positive",
            "slack:D123:1234567890.000000",
        )

    # TS: "exposes buildFeedbackButtonsBlock defaults"
    def test_exposes_buildfeedbackbuttonsblock_defaults(self):
        assert build_feedback_buttons_block() == {
            "type": "context_actions",
            "elements": [
                {
                    "type": "feedback_buttons",
                    "action_id": "message_feedback",
                    "positive_button": {"text": {"type": "plain_text", "text": "Good response"}, "value": "positive"},
                    "negative_button": {"text": {"type": "plain_text", "text": "Bad response"}, "value": "negative"},
                }
            ],
        }
