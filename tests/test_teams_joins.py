"""Teams bot joins (#217).

Port of upstream ``packages/adapter-teams/src/joins.test.ts`` › "Teams bot
joins" (vercel/chat ``aaeede70`` #899, chat@4.40.0), plus the case-insensitive
self check and the ``28:`` ``bot_user_id`` it introduced. Every activity goes
through the real webhook bridge (``handle_webhook``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from chat_sdk.adapters.teams.adapter import CACHE_TTL_MS, TeamsAdapter
from chat_sdk.adapters.teams.types import TeamsAdapterConfig, TeamsThreadId
from chat_sdk.chat import Chat
from chat_sdk.testing import MockLogger, create_mock_chat_instance, create_mock_state
from chat_sdk.types import ChatConfig, MemberJoinedChannelEvent, WebhookOptions
from tests._teams_harness import (
    APP_ID,
    BOT_ID,
    SERVICE_URL,
    allow_unauthenticated_webhooks,
    collecting_options,
    make_adapter,
    make_logger,
    receive,
    spy_activity_sender,
)

CONVERSATION_ID = "19:channel@thread.tacv2"


def activity() -> dict[str, Any]:
    return {
        "type": "conversationUpdate",
        "id": "join-activity",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": "29:inviter", "aadObjectId": "inviter-aad"},
        "recipient": {"id": BOT_ID},
        "conversation": {
            "id": CONVERSATION_ID,
            "conversationType": "channel",
            "isGroup": True,
            "tenantId": "tenant",
        },
        "membersAdded": [{"id": BOT_ID}],
        "channelData": {
            "eventType": "teamMemberAdded",
            "team": {"id": "19:team", "aadGroupId": "team-aad"},
            "settings": {"selectedChannel": {"id": CONVERSATION_ID}},
            "tenant": {"id": "tenant"},
        },
    }


def thread(conversation_id: str, conversation_type: Any = None) -> TeamsThreadId:
    return TeamsThreadId(conversation_id=conversation_id, service_url=SERVICE_URL, conversation_type=conversation_type)


@pytest.fixture(autouse=True)
def _skip_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    allow_unauthenticated_webhooks(monkeypatch)


class _Setup:
    def __init__(self, **overrides: Any) -> None:
        self.logger = make_logger()
        self.adapter = make_adapter(self.logger, **overrides)
        self.state = create_mock_state()
        self.chat = create_mock_chat_instance(state=self.state)
        self.options, self.tasks = collecting_options()

    async def init(self) -> _Setup:
        await self.adapter.initialize(self.chat)
        return self

    async def receive(self, body: dict[str, Any]) -> dict[str, Any]:
        return await receive(self.adapter, body, self.options)

    def joins(self) -> list[MemberJoinedChannelEvent]:
        calls = self.chat.process_member_joined_channel.call_args_list
        for call in calls:
            assert call.args[1] is self.options
        return [call.args[0] for call in calls]


class TestTeamsBotJoins:
    # TS: "exposes the configured bot identity"
    def test_exposes_the_configured_bot_identity(self) -> None:
        assert make_adapter().bot_user_id == BOT_ID

    # TS: "uses the resolved app identity with environment fallback" (env half;
    # the resolver half is covered by test_teams_connect.py's lazy-identity tests)
    def test_uses_the_resolved_app_identity_with_environment_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEAMS_APP_ID", "environment-app")
        fallback = TeamsAdapter(TeamsAdapterConfig(app_password="test", logger=make_logger()))
        explicit = make_adapter()
        assert fallback.bot_user_id == "28:environment-app"
        assert explicit.bot_user_id == BOT_ID

    # TS: "dispatches the captured channel-join shape through the SDK router"
    async def test_dispatches_the_captured_channel_join_shape_through_the_sdk_router(self) -> None:
        s = await _Setup().init()
        response = await s.receive(activity())
        assert response["status"] == 200
        assert s.joins() == [
            MemberJoinedChannelEvent(
                adapter=s.adapter,
                channel_id=s.adapter.encode_thread_id(thread(CONVERSATION_ID)),
                user_id=BOT_ID,
                inviter_id="29:inviter",
            )
        ]

    # TS: "caches Graph channel context from the team-install payload"
    async def test_caches_graph_channel_context_from_the_team_install_payload(self) -> None:
        s = _Setup()
        s.state.set = AsyncMock(side_effect=s.state.set)
        await s.init()
        await s.receive(activity())
        s.state.set.assert_any_await(
            f"teams:channelContext:{CONVERSATION_ID}",
            json.dumps({"team_id": "team-aad", "channel_id": CONVERSATION_ID}),
            CACHE_TTL_MS,
        )

    async def test_does_not_cache_team_context_for_a_non_channel_conversation_id(self) -> None:
        # The fallback is only the base ``19:`` conversation id (upstream).
        s = await _Setup().init()
        body = activity()
        await s.receive({**body, "conversation": {**body["conversation"], "id": "a:personal"}})
        assert "teams:channelContext:a:personal" not in s.state.cache

    # TS: "dispatches group-chat joins without channelData"
    async def test_dispatches_group_chat_joins_without_channel_data(self) -> None:
        s = await _Setup().init()
        await s.receive(
            {
                **activity(),
                "conversation": {"id": "group-chat", "conversationType": "groupChat", "isGroup": True},
                "channelData": None,
            }
        )
        [join] = s.joins()
        assert join.channel_id == s.adapter.encode_thread_id(thread("group-chat", "groupChat"))
        assert join.user_id == BOT_ID

    # TS: "uses group metadata when conversationType is absent"
    async def test_uses_group_metadata_when_conversation_type_is_absent(self) -> None:
        s = await _Setup().init()
        body = activity()
        await s.receive({**body, "conversation": {**body["conversation"], "conversationType": None}})
        assert len(s.joins()) == 1

    # TS: "falls back to the 19: prefix when the conversation type is unresolved"
    async def test_falls_back_to_the_19_prefix_when_the_conversation_type_is_unresolved(self) -> None:
        s = await _Setup().init()
        await s.receive({**activity(), "conversation": {"id": "19:unknown@thread.tacv2"}, "channelData": None})
        [join] = s.joins()
        assert join.channel_id == s.adapter.encode_thread_id(thread("19:unknown@thread.tacv2"))
        assert join.user_id == BOT_ID

    # TS: "matches the bot identity regardless of app ID casing"
    async def test_matches_the_bot_identity_regardless_of_app_id_casing(self) -> None:
        upper_app_id = APP_ID.upper()
        s = await _Setup(app_id=upper_app_id).init()
        await s.receive(activity())
        [join] = s.joins()
        assert join.user_id == f"28:{upper_app_id}"
        assert s.adapter.bot_user_id == f"28:{upper_app_id}"

    # TS: it.each "does not emit a bot join for %j"
    @pytest.mark.parametrize(
        "overrides",
        [
            {"membersAdded": [{"id": "29:user"}]},
            {"membersAdded": []},
            {"membersAdded": None, "membersRemoved": [{"id": BOT_ID}]},
            {"recipient": {"id": "28:other-app"}},
            {"conversation": {"id": "personal", "conversationType": "personal"}},
            {"conversation": {"id": "a:unknown"}},
            {"conversation": {"id": "", "conversationType": "channel"}},
            {"serviceUrl": ""},
            {"type": "installationUpdate", "action": "add"},
            {"type": "installationUpdate", "action": "remove"},
            # membersAdded is compared exactly against the platform-supplied
            # recipient id (upstream), so a recased copy is not the bot.
            {"membersAdded": [{"id": BOT_ID.upper()}]},
        ],
    )
    async def test_does_not_emit_a_bot_join_for_j(self, overrides: dict[str, Any]) -> None:
        s = await _Setup().init()
        response = await s.receive({**activity(), **overrides})
        assert response["status"] == 200
        s.chat.process_member_joined_channel.assert_not_called()

    # TS: "logs the skip reason at debug level"
    async def test_logs_the_skip_reason_at_debug_level(self) -> None:
        s = await _Setup().init()
        await s.receive({**activity(), "membersAdded": [{"id": "29:user"}]})
        s.logger.debug.assert_any_call(
            "Ignoring conversationUpdate",
            {"activityId": "join-activity", "reason": "bot was not among the added members"},
        )

    # TS: "warns instead of silently dropping joins when no app ID is configured"
    async def test_warns_instead_of_silently_dropping_joins_when_no_app_id_is_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEAMS_APP_ID", "")
        logger = make_logger()
        unconfigured = TeamsAdapter(TeamsAdapterConfig(app_password="test", logger=logger))
        chat = create_mock_chat_instance()
        await unconfigured.initialize(chat)
        assert unconfigured.bot_user_id is None
        logger.warn.reset_mock()

        response = await receive(unconfigured, activity())
        assert response["status"] == 200
        chat.process_member_joined_channel.assert_not_called()
        logger.warn.assert_called_once_with(
            "Teams app ID is not configured, ignoring conversationUpdate. "
            "Set appId or TEAMS_APP_ID to receive bot join events."
        )

    # TS: "dispatches only the bot when several members are added"
    async def test_dispatches_only_the_bot_when_several_members_are_added(self) -> None:
        s = await _Setup().init()
        await s.receive({**activity(), "membersAdded": [{"id": "29:user"}, {"id": BOT_ID}, {"id": BOT_ID}]})
        assert len(s.joins()) == 1

    # TS: "passes handleWebhook options to the join through the bridge"
    async def test_passes_handle_webhook_options_to_the_join_through_the_bridge(self) -> None:
        # Each webhook's options reach its own join, never another's.
        s = await _Setup().init()
        first = WebhookOptions(wait_until=lambda task: None)
        second = WebhookOptions(wait_until=lambda task: None)
        await receive(s.adapter, activity(), first)
        await receive(s.adapter, {**activity(), "id": "second-join"}, second)
        await receive(s.adapter, {**activity(), "id": "no-options"})
        calls = s.chat.process_member_joined_channel.call_args_list
        assert [call.args[1] for call in calls] == [first, second, None]
        assert calls[0].args[1] is first
        assert calls[1].args[1] is second

    # TS: "runs an asynchronous welcome handler using the existing channel API"
    async def test_runs_an_asynchronous_welcome_handler_using_the_existing_channel_api(self) -> None:
        logger = MockLogger()
        runtime = make_adapter(logger)
        bot = Chat(ChatConfig(user_name="test", adapters={"teams": runtime}, state=create_mock_state(), logger=logger))
        received: list[MemberJoinedChannelEvent] = []

        async def handler(event: MemberJoinedChannelEvent) -> None:
            received.append(event)
            await asyncio.sleep(0)
            await bot.channel(event.channel_id).post("Welcome")

        bot.on_member_joined_channel(handler)
        await bot.initialize()
        send = spy_activity_sender(runtime)
        options, tasks = collecting_options()

        await receive(runtime, activity(), options)
        assert len(tasks) == 1
        await asyncio.gather(*tasks)
        [event] = received
        assert event.user_id == BOT_ID
        assert event.adapter.bot_user_id == BOT_ID
        send.assert_awaited_once()
        sent, ref = send.call_args.args
        assert sent.text == "Welcome"
        assert ref.conversation.id == CONVERSATION_ID
        assert ref.service_url == SERVICE_URL.rstrip("/")


class TestTeamsBotIdentity:
    """Consumer-visible effects of the ``28:`` id and the case-insensitive self check."""

    def test_self_check_ignores_app_id_casing(self) -> None:
        adapter = make_adapter()
        base = {
            "type": "message",
            "id": "m1",
            "text": "hi",
            "serviceUrl": SERVICE_URL,
            "conversation": {"id": CONVERSATION_ID, "conversationType": "channel"},
        }
        for from_id in (BOT_ID, BOT_ID.upper(), APP_ID.upper(), f"28:{APP_ID.upper()}"):
            assert adapter.parse_message({**base, "from": {"id": from_id}}).author.is_me is True, from_id
        for from_id in ("29:user", f"28:{APP_ID}-other", APP_ID[:-1]):
            assert adapter.parse_message({**base, "from": {"id": from_id}}).author.is_me is False, from_id

    async def test_text_mention_detection_uses_the_28_prefixed_id(self) -> None:
        logger = MockLogger()
        adapter = make_adapter(logger)
        bot = Chat(ChatConfig(user_name="test", adapters={"teams": adapter}, state=create_mock_state(), logger=logger))
        base = {
            "type": "message",
            "id": "m1",
            "serviceUrl": SERVICE_URL,
            "from": {"id": "29:user"},
            "conversation": {"id": CONVERSATION_ID, "conversationType": "channel"},
        }
        mentioned = adapter.parse_message({**base, "text": f"hey @{BOT_ID} ping"})
        bare = adapter.parse_message({**base, "text": f"hey @{APP_ID} ping"})
        assert bot._detect_mention(adapter, mentioned) is True
        assert bot._detect_mention(adapter, bare) is False
