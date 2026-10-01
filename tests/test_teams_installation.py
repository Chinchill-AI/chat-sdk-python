"""Teams installation lifecycle (#217).

Port of upstream ``packages/adapter-teams/src/installation.test.ts`` ›
"Teams installation lifecycle" (vercel/chat ``2e2426d1`` #914, chat@4.41.0).
Every activity goes through the real webhook bridge (``handle_webhook``).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import ANY

import pytest

from chat_sdk.adapters.teams.installation import INSTALLATION_ACTIONS, is_install_action, parse_installation_action
from chat_sdk.adapters.teams.types import TeamsThreadId
from chat_sdk.chat import Chat
from chat_sdk.testing import MockLogger, create_mock_chat_instance, create_mock_state
from chat_sdk.types import ChatConfig, InstallationEvent, InstalledEvent, UninstalledEvent
from tests._teams_harness import (
    BOT_ID,
    SERVICE_URL,
    accept_test_signing_key,
    allow_unauthenticated_webhooks,
    bot_framework_token,
    collecting_options,
    make_adapter,
    make_logger,
    receive,
    spy_activity_sender,
)


def activity(action: str = "add") -> dict[str, Any]:
    """Synthetic installationUpdate modeled on Microsoft's documented schema.

    No captured tenant traffic exists for this activity type (upstream note).
    """
    return {
        "type": "installationUpdate",
        "id": f"installation-{action}",
        "action": action,
        "channelId": "msteams",
        "locale": "en-US",
        "serviceUrl": SERVICE_URL,
        "from": {"id": "29:installer", "aadObjectId": "installer-aad", "name": "Installer"},
        "recipient": {"id": BOT_ID, "name": "Bot"},
        "conversation": {
            "id": "personal-installation",
            "conversationType": "personal",
            "tenantId": "tenant",
        },
        "channelData": {"tenant": {"id": "tenant"}},
    }


def thread(conversation_id: str, service_url: str = SERVICE_URL, conversation_type: Any = None) -> TeamsThreadId:
    return TeamsThreadId(conversation_id=conversation_id, service_url=service_url, conversation_type=conversation_type)


@pytest.fixture
def _skip_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    allow_unauthenticated_webhooks(monkeypatch)


class _Setup:
    def __init__(self) -> None:
        self.logger = make_logger()
        self.adapter = make_adapter(self.logger)
        self.chat = create_mock_chat_instance()
        self.options, self.tasks = collecting_options()

    async def init(self) -> _Setup:
        await self.adapter.initialize(self.chat)
        return self

    async def receive(self, body: dict[str, Any]) -> dict[str, Any]:
        return await receive(self.adapter, body, self.options)


@pytest.mark.usefixtures("_skip_auth")
class TestTeamsInstallationLifecycle:
    # TS: it.each "routes %s exactly once through the SDK"
    @pytest.mark.parametrize(
        ("action", "processor", "other", "event_class"),
        [
            ("add", "process_installed", "process_uninstalled", InstalledEvent),
            ("add-upgrade", "process_installed", "process_uninstalled", InstalledEvent),
            ("remove", "process_uninstalled", "process_installed", UninstalledEvent),
            ("remove-upgrade", "process_uninstalled", "process_installed", UninstalledEvent),
        ],
    )
    async def test_routes_s_exactly_once_through_the_sdk(
        self, action: str, processor: str, other: str, event_class: type[InstallationEvent]
    ) -> None:
        s = await _Setup().init()
        body = activity(action)
        response = await s.receive(body)
        assert response["status"] == 200
        process = getattr(s.chat, processor)
        process.assert_called_once()
        event, options = process.call_args.args
        assert type(event) is event_class
        assert event == event_class(
            adapter=s.adapter,
            id=body["id"],
            action=action,  # type: ignore[arg-type]
            conversation_id="personal-installation",
            channel_id=s.adapter.encode_thread_id(thread("personal-installation", conversation_type="personal")),
            user_id="29:installer",
            tenant_id="tenant",
            locale="en-US",
            raw=ANY,
        )
        assert event.raw["action"] == action
        assert options is s.options
        s.chat.process_member_joined_channel.assert_not_called()
        getattr(s.chat, other).assert_not_called()

    # TS: "preserves identifiable removal when actor and locale are absent"
    async def test_preserves_identifiable_removal_when_actor_and_locale_are_absent(self) -> None:
        s = await _Setup().init()
        await s.receive({**activity("remove"), "from": None, "locale": None})
        s.chat.process_uninstalled.assert_called_once()
        event = s.chat.process_uninstalled.call_args.args[0]
        assert event.conversation_id == "personal-installation"
        assert event.user_id is None
        assert event.locale is None

    # TS: it.each "ignores malformed or unknown lifecycle activities: %j"
    @pytest.mark.parametrize(
        "overrides",
        [
            {"action": "future-action"},
            {"recipient": {"id": "28:another-bot"}},
            {"recipient": {"id": ""}},
            {"conversation": {"id": ""}},
            # Python-specific: a non-string action is not trusted either.
            {"action": ["add"]},
        ],
    )
    async def test_ignores_malformed_or_unknown_lifecycle_activities_j(self, overrides: dict[str, Any]) -> None:
        s = await _Setup().init()
        response = await s.receive({**activity(), **overrides})
        assert response["status"] == 200
        s.chat.process_installed.assert_not_called()
        s.chat.process_uninstalled.assert_not_called()
        messages = [c.args[0] for c in s.logger.debug.call_args_list]
        assert any(m.startswith("Ignoring installationUpdate: ") for m in messages), messages

    async def test_prefers_the_conversation_tenant_over_channel_data(self) -> None:
        # Upstream ``tenantIdFromActivity``: ``conversation.tenantId ?? channelData.tenant.id``.
        s = await _Setup().init()
        body = activity()
        await s.receive(
            {
                **body,
                "conversation": {**body["conversation"], "tenantId": "conv-tenant"},
                "channelData": {"tenant": {"id": "cd-tenant"}},
            }
        )
        s.chat.process_installed.assert_called_once()
        assert s.chat.process_installed.call_args.args[0].tenant_id == "conv-tenant"

    # TS: it.each "preserves %s location, tenant, and classification"
    @pytest.mark.parametrize("conversation_type", ["channel", "groupChat"])
    async def test_preserves_s_location_tenant_and_classification(self, conversation_type: str) -> None:
        s = await _Setup().init()
        conversation_id = "19:selected@thread.tacv2"
        body = {
            **activity(),
            # Team and group payloads carry the tenant only in channelData.
            "conversation": {"id": conversation_id, "conversationType": conversation_type, "isGroup": True},
            "channelData": {
                "tenant": {"id": "tenant"},
                "team": {"id": "19:team", "aadGroupId": "team-aad"},
                "settings": {"selectedChannel": {"id": conversation_id}},
            },
        }
        await s.receive(body)
        s.chat.process_installed.assert_called_once()
        event = s.chat.process_installed.call_args.args[0]
        assert event.conversation_id == conversation_id
        assert event.tenant_id == "tenant"
        assert event.channel_id == s.adapter.encode_thread_id(
            thread(conversation_id, conversation_type=conversation_type)
        )
        assert event.raw["channelData"] == body["channelData"]

    async def test_keeps_the_conversation_type_when_the_prefix_heuristic_disagrees(self) -> None:
        # An ``a:`` group chat would read as a DM from its prefix alone; the
        # persisted channel_id must carry the type so later posts route right.
        s = await _Setup().init()
        await s.receive({**activity(), "conversation": {"id": "a:group", "conversationType": "groupChat"}})
        channel_id = s.chat.process_installed.call_args.args[0].channel_id
        assert channel_id.endswith(":groupChat")
        assert s.adapter.decode_thread_id(channel_id) == thread("a:group", conversation_type="groupChat")
        assert s.adapter.is_dm(channel_id) is False

    # TS: "emits both a join and an install for a team installation"
    async def test_emits_both_a_join_and_an_install_for_a_team_installation(self) -> None:
        s = await _Setup().init()
        conversation_id = "19:selected@thread.tacv2"
        body = {
            **activity(),
            "conversation": {"id": conversation_id, "conversationType": "channel", "isGroup": True},
            "channelData": {"tenant": {"id": "tenant"}, "team": {"id": "19:team"}},
        }
        await s.receive(body)
        await s.receive({**body, "type": "conversationUpdate", "id": "join", "membersAdded": [{"id": BOT_ID}]})
        channel_id = s.adapter.encode_thread_id(thread(conversation_id, conversation_type="channel"))
        s.chat.process_installed.assert_called_once()
        assert s.chat.process_installed.call_args.args[0].channel_id == channel_id
        assert s.chat.process_installed.call_args.args[1] is s.options
        s.chat.process_member_joined_channel.assert_called_once()
        assert s.chat.process_member_joined_channel.call_args.args[0].channel_id == channel_id
        assert s.chat.process_member_joined_channel.call_args.args[1] is s.options

    # TS: "allows older custom Chat instances to omit lifecycle processors"
    async def test_allows_older_custom_chat_instances_to_omit_lifecycle_processors(self) -> None:
        s = _Setup()
        del s.chat.process_installed
        del s.chat.process_uninstalled
        await s.init()
        assert (await s.receive(activity()))["status"] == 200
        assert (await s.receive(activity("remove")))["status"] == 200
        s.logger.error.assert_not_called()

    # TS: "tracks asynchronous installation work through the actual webhook bridge"
    async def test_tracks_asynchronous_installation_work_through_the_actual_webhook_bridge(self) -> None:
        logger = MockLogger()
        runtime = make_adapter(logger)
        bot = Chat(ChatConfig(user_name="bot", adapters={"teams": runtime}, state=create_mock_state(), logger=logger))
        gate = asyncio.Event()
        done: list[bool] = []

        async def on_installed(event: InstalledEvent) -> None:
            await gate.wait()
            done.append(True)

        bot.on_installed(on_installed)
        await bot.initialize()
        options, tasks = collecting_options()
        response = await receive(runtime, activity(), options)
        assert response["status"] == 200
        assert len(tasks) == 1
        await asyncio.sleep(0)
        assert done == []
        gate.set()
        await asyncio.gather(*tasks)
        assert done == [True]

    # TS: "persists channel IDs, replaces reinstalls, and posts later to each service URL"
    async def test_persists_channel_ids_replaces_reinstalls_and_posts_later_to_each_service_url(self) -> None:
        saved: dict[str, str] = {}
        logger = MockLogger()
        runtime = make_adapter(logger)
        bot = Chat(ChatConfig(user_name="bot", adapters={"teams": runtime}, state=create_mock_state(), logger=logger))

        def key(event: InstallationEvent) -> str:
            return f"{event.tenant_id}:{event.conversation_id}"

        def on_installed(event: InstalledEvent) -> None:
            if event.channel_id:
                saved[key(event)] = event.channel_id

        def on_uninstalled(event: UninstalledEvent) -> None:
            saved.pop(key(event), None)

        bot.on_installed(on_installed)
        bot.on_uninstalled(on_uninstalled)
        await bot.initialize()
        send = spy_activity_sender(runtime)
        options, tasks = collecting_options()

        await receive(runtime, activity(), options)
        await asyncio.gather(*tasks)
        first = saved["tenant:personal-installation"]
        next_service_url = "https://smba.trafficmanager.net/emea/"
        await receive(runtime, {**activity(), "id": "reinstall", "serviceUrl": next_service_url}, options)
        await asyncio.gather(*tasks)
        second = saved["tenant:personal-installation"]
        assert second != first

        # Only the persisted thread ID is needed to reach each installation later.
        for channel_id, endpoint in ((first, SERVICE_URL), (second, next_service_url)):
            await bot.channel(channel_id).post("Welcome back")
            sent, ref = send.call_args.args
            assert sent.text == "Welcome back"
            assert ref.service_url == endpoint.rstrip("/")
            assert ref.conversation.id == "personal-installation"

        await receive(runtime, {**activity("remove"), "serviceUrl": None}, options)
        await asyncio.gather(*tasks)
        assert saved == {}
        assert send.await_count == 2


class TestTeamsInstallationTokenServiceUrl:
    """The service-URL fallback needs a real validated token, so no skip-auth here."""

    @pytest.fixture(autouse=True)
    def _signing_key(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        accept_test_signing_key(monkeypatch, key)
        self.key = key
        return key

    # TS: it.each "falls back to the token service URL when %s omits one"
    @pytest.mark.parametrize(("action", "processor"), [("add", "process_installed"), ("remove", "process_uninstalled")])
    async def test_falls_back_to_the_token_service_url_when_s_omits_one(self, action: str, processor: str) -> None:
        s = await _Setup().init()
        token = bot_framework_token(self.key)
        response = await receive(s.adapter, {**activity(action), "serviceUrl": None}, s.options, token=token)
        assert response["status"] == 200
        process = getattr(s.chat, processor)
        process.assert_called_once()
        event, options = process.call_args.args
        assert event.conversation_id == "personal-installation"
        # The SDK token strips the trailing slash, as upstream's reference does.
        assert event.channel_id == s.adapter.encode_thread_id(
            thread("personal-installation", SERVICE_URL.rstrip("/"), "personal")
        )
        assert options is s.options

    async def test_restores_the_root_slash_the_token_strips_from_a_root_path_endpoint(self) -> None:
        # Python-specific: the SDK token turns ``https://host/`` into
        # ``https://host``, which the SSRF allow-list would otherwise reject.
        s = await _Setup().init()
        root_url = "https://smba.infra.gcc.teams.microsoft.com/"
        token = bot_framework_token(self.key, service_url=root_url)
        await receive(s.adapter, {**activity(), "serviceUrl": None}, s.options, token=token)
        s.chat.process_installed.assert_called_once()
        assert s.chat.process_installed.call_args.args[0].channel_id == s.adapter.encode_thread_id(
            thread("personal-installation", root_url, "personal")
        )

    async def test_ignores_a_disallowed_token_service_url(self) -> None:
        # Python-specific: the token fallback is checked against the SSRF
        # allow-list; a disallowed one leaves channel_id None, never persisted.
        s = await _Setup().init()
        token = bot_framework_token(self.key, service_url="https://attacker.example/")
        await receive(s.adapter, {**activity(), "serviceUrl": None}, s.options, token=token)
        s.chat.process_installed.assert_called_once()
        assert s.chat.process_installed.call_args.args[0].channel_id is None
        s.logger.warn.assert_any_call(
            "Ignoring disallowed token serviceUrl", {"serviceUrl": "https://attacker.example"}
        )


class TestInstallationActionParsing:
    def test_accepts_exactly_the_documented_wire_actions(self) -> None:
        assert INSTALLATION_ACTIONS == ("add", "add-upgrade", "remove", "remove-upgrade")
        for action in INSTALLATION_ACTIONS:
            assert parse_installation_action(action) == action
        for value in ("Add", "add ", "", None, 1, ["add"]):
            assert parse_installation_action(value) is None
        assert [a for a in INSTALLATION_ACTIONS if is_install_action(a)] == ["add", "add-upgrade"]
