"""Slack inbound mentions and authors: self-mention decoding, content-based
``is_mention``, and author ids / email / ``is_system``.

Port of the ``packages/adapter-slack/src/index.test.ts`` cases added by
upstream vercel/chat#891 (51322dde), #947 (683eadc1), #883 (c2b6bff0),
#716 (bb7cd124) and #707 (80def3ab).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.slack.adapter import SlackAdapter, _attachment_content, _AttachmentPart
from chat_sdk.adapters.slack.types import RequestContext, SlackAdapterConfig
from chat_sdk.chat import Chat
from chat_sdk.state.memory import create_memory_state
from chat_sdk.types import ChatConfig, Message, WebhookOptions

SECRET = "test-signing-secret"
ANY_TEXT_PATTERN = re.compile(r".")


@pytest.fixture(autouse=True)
def _no_unfurl_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Messages with links poll the unfurl cache; nothing here caches one."""
    monkeypatch.setattr("chat_sdk.adapters.slack.adapter._UNFURL_WAIT_MS", 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Client:
    """Slack Web API stand-in: ``users.info`` answers per user id."""

    def __init__(
        self,
        names: dict[str, str] | None = None,
        *,
        default_name: str = "User",
        fail: frozenset[str] = frozenset(),
        emails: dict[str, str] | None = None,
        auth: dict[str, Any] | None = None,
    ) -> None:
        self._names = names or {}
        self._default_name = default_name
        self._fail = fail
        self._emails = emails or {}
        self.users_info = AsyncMock(side_effect=self._users_info)
        self.conversations_info = AsyncMock(return_value={"ok": True, "channel": {"name": "general"}})
        self.auth_test = AsyncMock(return_value=auth if auth is not None else {"ok": True})
        self.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "2222222222.000000"})

    async def _users_info(self, *, user: str) -> dict[str, Any]:
        if user in self._fail:
            raise RuntimeError("rate_limited")
        name = self._names.get(user, self._default_name)
        profile: dict[str, Any] = {"display_name": name, "real_name": name}
        if user in self._emails:
            profile["email"] = self._emails[user]
        return {"ok": True, "user": {"name": name.lower(), "real_name": name, "profile": profile}}


def _make_adapter(client: _Client, **overrides: Any) -> SlackAdapter:
    config = SlackAdapterConfig(signing_secret=SECRET, bot_token="xoxb-test-token", **overrides)
    adapter = SlackAdapter(config)
    adapter._get_client = lambda token=None: client  # type: ignore[assignment]
    return adapter


def _make_mock_chat() -> MagicMock:
    state = MagicMock()
    state.get = AsyncMock(return_value=None)
    state.set = AsyncMock()
    state.get_list = AsyncMock(return_value=[])
    state.append_to_list = AsyncMock()
    chat = MagicMock()
    chat.process_message = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    return chat


class _FakeRequest:
    def __init__(self, body: str) -> None:
        ts = str(int(time.time()))
        sig = "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
        self.body = body.encode("utf-8")
        self.headers = {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
            "content-type": "application/json",
        }
        self.url = ""

    async def text(self) -> str:
        return self.body.decode("utf-8")


def _webhook(event: dict[str, Any]) -> _FakeRequest:
    return _FakeRequest(json.dumps({"type": "event_callback", "team_id": "T123", "event": event}))


async def _parse_incoming(
    event: dict[str, Any],
    client: _Client | None = None,
    **adapter_overrides: Any,
) -> Message:
    """Round-trip an event through the webhook and return the parsed message."""
    if "bot_user_id" not in adapter_overrides:
        adapter_overrides["bot_user_id"] = "U_BOT"
    client = client or _Client({"U_BOT": "Test Bot"})
    adapter = _make_adapter(client, **adapter_overrides)
    chat = _make_mock_chat()
    await adapter.initialize(chat)
    await adapter.handle_webhook(_webhook(event))
    factory = chat.process_message.call_args.args[2]
    return await factory()


def _rich_text(*elements: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": list(elements)}]}]


def _code_only_blocks() -> list[dict[str, Any]]:
    return _rich_text(
        {"type": "text", "text": "the app imports "},
        {"type": "text", "text": "<@U_BOT>/passport", "style": {"code": True}},
    )


def _raw_text_table() -> list[dict[str, Any]]:
    # Slack's flattened fallback repeats the cell text, which is not a mention
    # because a ``raw_text`` cell renders literally.
    row = [{"type": "raw_text", "text": "Import"}, {"type": "raw_text", "text": "<@U_BOT>/passport"}]
    return [{"type": "table", "rows": [row]}]


# ---------------------------------------------------------------------------
# parse_message (sync path)
# ---------------------------------------------------------------------------


class TestParseMessage:
    def _adapter(self, bot_user_id: str | None = "U_BOT") -> SlackAdapter:
        overrides = {"bot_user_id": bot_user_id} if bot_user_id else {}
        return _make_adapter(_Client(), **overrides)

    def test_classifies_the_bots_mention_without_a_user_lookup(self):
        adapter = self._adapter()
        base = {
            "type": "app_mention",
            "user": "U123",
            "channel": "C456",
            "text": "<@U_BOT> hi",
            "ts": "1234567890.123456",
        }
        mention = adapter.parse_message({**base, "blocks": _rich_text({"type": "user", "user_id": "U_BOT"})})
        code = adapter.parse_message(
            {**base, "blocks": _rich_text({"type": "text", "text": "<@U_BOT> hi", "style": {"code": True}})}
        )
        assert mention.is_mention is True
        assert code.is_mention is False

    def test_matches_a_structured_user_element_regardless_of_bot_id_case(self):
        adapter = self._adapter("u_bot")
        message = adapter.parse_message(
            {
                "type": "message",
                "user": "U123",
                "channel": "C456",
                "text": "<@U_BOT> hi",
                "ts": "1234567890.123456",
                "blocks": _rich_text({"type": "user", "user_id": "U_BOT"}),
            }
        )
        assert message.is_mention is True

    def test_converts_special_mentions_to_readable_text(self):
        message = self._adapter().parse_message(
            {
                "type": "message",
                "user": "U123",
                "channel": "C456",
                "text": "<!here> and <!subteam^S0123456789|@devs>",
                "ts": "1234567890.123456",
            }
        )
        assert message.text == "@here and @devs"
        assert [node["type"] for node in message.formatted["children"]] == ["paragraph"]

    def test_preserves_special_mention_tokens_in_an_inbound_inline_code_span(self):
        text = "<!here> <!channel> <!everyone> <!subteam^S123>"
        message = self._adapter().parse_message(
            {
                "type": "message",
                "user": "U123",
                "channel": "D456",
                "channel_type": "im",
                "text": f"review code `{text}`",
                "ts": "1234567890.123456",
                "blocks": _rich_text(
                    {"type": "text", "text": "review code "},
                    {"type": "text", "text": text, "style": {"code": True}},
                ),
            }
        )
        assert message.text == f"review code {text}"
        paragraph = message.formatted["children"]
        assert [node["type"] for node in paragraph] == ["paragraph"]
        assert [(c["type"], c["value"]) for c in paragraph[0]["children"]] == [
            ("text", "review code "),
            ("inlineCode", text),
        ]
        assert message.is_mention is False

    def test_preserves_special_mention_tokens_in_an_inbound_code_block(self):
        text = "review fence <!here> <!channel> <!everyone>"
        message = self._adapter().parse_message(
            {
                "type": "message",
                "user": "U123",
                "channel": "D456",
                "channel_type": "im",
                "text": f"```{text}```",
                "ts": "1234567890.123456",
                "blocks": [
                    {
                        "type": "rich_text",
                        "elements": [
                            {
                                "type": "rich_text_preformatted",
                                "elements": [{"type": "text", "text": text}],
                                "border": 0,
                            }
                        ],
                    }
                ],
            }
        )
        assert message.text == text
        assert [(n["type"], n["value"]) for n in message.formatted["children"]] == [("code", text)]
        assert message.is_mention is False

    def test_uses_the_bot_user_id_instead_of_the_app_bot_id(self):
        message = self._adapter().parse_message(
            {
                "type": "message",
                "bot_id": "B123",
                "bot_profile": {"user_id": "U123"},
                "channel": "C456",
                "text": "Bot message",
                "ts": "1234567890.123456",
                "subtype": "bot_message",
            }
        )
        assert message.author.user_id == "U123"
        assert message.author.is_bot is True

    def test_malformed_bot_profile_falls_back_to_the_app_bot_id(self):
        """Python-specific guard: a non-dict ``bot_profile`` is ignored."""
        message = self._adapter().parse_message(
            {
                "type": "message",
                "bot_id": "B123",
                "bot_profile": "U123",
                "channel": "C456",
                "text": "Bot message",
                "ts": "1234567890.123456",
            }
        )
        assert message.author.user_id == "B123"

    def test_marks_uslack_messages_as_system_authored(self):
        message = self._adapter().parse_message(
            {
                "type": "message",
                "user": "USLACK",
                "channel": "D456",
                "channel_type": "im",
                "text": "<@U123> archived the channel <#C123>",
                "ts": "1234567890.123456",
            }
        )
        author = message.author
        assert (author.user_id, author.is_bot, author.is_system, author.is_me) == ("USLACK", False, True, False)

    def test_reports_none_for_a_non_mention_when_the_bot_id_is_unknown(self):
        """Python-specific: the tri-state stays undetermined (``None``, not
        ``False``) so the core text fallback decides."""
        message = self._adapter(None).parse_message(
            {"type": "message", "user": "U123", "channel": "C456", "text": "hello", "ts": "1.1"}
        )
        assert message.is_mention is None

    def test_uses_the_request_scoped_bot_id_for_classification(self):
        """Python-specific: multi-workspace requests carry the bot id in the
        request context, not on the adapter."""
        adapter = _make_adapter(_Client())
        token = adapter._request_context.set(RequestContext(token="xoxb-multi", bot_user_id="U_BOT_MULTI"))
        try:
            message = adapter.parse_message(
                {"type": "message", "user": "U123", "channel": "C456", "text": "<@U_BOT_MULTI> hi", "ts": "1.1"}
            )
        finally:
            adapter._request_context.reset(token)
        assert message.is_mention is True


# ---------------------------------------------------------------------------
# Incoming webhooks: self-mention decoding and content classification
# ---------------------------------------------------------------------------


class TestResolveInlineMentions:
    async def test_resolves_the_bots_own_mention_and_flags_it_in_incoming_webhooks(self):
        message = await _parse_incoming(
            {
                "type": "app_mention",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "<@U_BOT> help me",
                "ts": "1234567890.666666",
            },
            _Client(default_name="Test User"),
        )
        assert message.text == "@Test User help me"
        assert message.is_mention is True

    async def test_flags_the_bots_own_mention_in_rich_text_table_cells(self):
        # Upstream also asserts ``text == "@Test Bot"``; rendering table
        # blocks into the message body is #210.
        cell = _rich_text({"type": "user", "user_id": "U_BOT"})[0]
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "",
                "ts": "1234567890.676767",
                "blocks": [{"type": "table", "rows": [[cell]]}],
            }
        )
        assert message.is_mention is True

    async def test_does_not_flag_a_bot_id_that_only_appears_in_inline_code(self):
        message = await _parse_incoming(
            {
                "type": "app_mention",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "the app imports `<@U_BOT>/passport`",
                "ts": "1234567890.888888",
                "blocks": _code_only_blocks(),
            }
        )
        assert message.is_mention is False

    async def test_does_not_flag_a_code_styled_user_element(self):
        message = await _parse_incoming(
            {
                "type": "app_mention",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "the app imports `<@U_BOT>/passport` and A &amp; B",
                "ts": "1234567890.888889",
                "blocks": _rich_text(
                    {"type": "text", "text": "the app imports "},
                    {"type": "user", "user_id": "U_BOT", "style": {"code": True}},
                    {"type": "text", "text": "/passport", "style": {"code": True}},
                    {"type": "text", "text": " and A & B"},
                ),
            }
        )
        assert message.is_mention is False

    async def test_does_not_flag_a_bot_id_inside_a_preformatted_block(self):
        message = await _parse_incoming(
            {
                "type": "app_mention",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "```\n<@U_BOT> is the bot\n```",
                "ts": "1234567890.999999",
                "blocks": [
                    {
                        "type": "rich_text",
                        "elements": [
                            {
                                "type": "rich_text_preformatted",
                                "elements": [{"type": "text", "text": "<@U_BOT> is the bot"}],
                            }
                        ],
                    }
                ],
            }
        )
        assert message.is_mention is False

    async def test_flags_a_real_mention_when_the_same_message_also_shows_code(self):
        message = await _parse_incoming(
            {
                "type": "app_mention",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "<@U_BOT> run `<@U_BOT>/passport`",
                "ts": "1234567890.101010",
                "blocks": _rich_text(
                    {"type": "user", "user_id": "U_BOT"},
                    {"type": "text", "text": " run "},
                    {"type": "text", "text": "<@U_BOT>/passport", "style": {"code": True}},
                ),
            }
        )
        assert message.is_mention is True

    async def test_does_not_flag_a_bot_id_inside_a_code_span_when_no_blocks_are_present(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "run `<@U_BOT>` to invoke the bot",
                "ts": "1234567890.121212",
            }
        )
        assert message.is_mention is False

    async def test_does_not_flag_a_bot_id_inside_a_fenced_block_when_no_blocks_are_present(self):
        message = await _parse_incoming(
            {"type": "message", "user": "U_SENDER", "channel": "C456", "text": "```\n<@U_BOT>\n```", "ts": "1.131313"}
        )
        assert message.is_mention is False

    async def test_does_not_flag_a_literal_bot_name_inside_structured_code(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "the docs say `@Example Bot`",
                "ts": "1234567890.141414",
                "blocks": _rich_text(
                    {"type": "text", "text": "the docs say "},
                    {"type": "text", "text": "@Example Bot", "style": {"code": True}},
                ),
            }
        )
        assert message.is_mention is False

    async def test_does_not_flag_an_escaped_bot_id(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "the raw markup is &lt;@U_BOT&gt;",
                "ts": "1234567890.151515",
            }
        )
        assert message.is_mention is False

    async def test_does_not_flag_a_bot_id_inside_a_code_span_in_an_attachment(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "",
                "ts": "1234567890.161616",
                "attachments": [{"mrkdwn_in": ["text"], "text": "example: `<@U_BOT>`"}],
            }
        )
        assert message.is_mention is False

    async def test_flags_a_bot_mention_in_attachment_mrkdwn(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "",
                "ts": "1234567890.171717",
                "attachments": [{"mrkdwn_in": ["text"], "text": "hey <@U_BOT> take a look"}],
            }
        )
        assert message.is_mention is True

    async def test_flags_a_bot_mention_in_a_literal_attachment_part(self):
        """Python addition: a part not named in ``mrkdwn_in`` still honors
        ``<@U…>`` control sequences (backticks are not code there), and
        unfurl attachments are not the author's content."""
        literal = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "",
                "ts": "1.1",
                "attachments": [{"text": "example: `<@U_BOT>`"}],
            }
        )
        unfurl = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "",
                "ts": "1.2",
                "attachments": [{"text": "hey <@U_BOT>", "from_url": "https://example.com"}],
            }
        )
        assert literal.is_mention is True
        assert unfurl.is_mention is False

    async def test_does_not_let_unmarked_fallback_text_override_structured_code(self):
        message = await _parse_incoming(
            {
                "type": "app_mention",
                "user": "U_SENDER",
                "channel": "C456",
                # Slack's flattened fallback drops the code formatting.
                "text": "the app imports <@U_BOT>/passport",
                "ts": "1234567890.181818",
                "blocks": _code_only_blocks(),
            }
        )
        assert message.is_mention is False

    async def test_does_not_flag_a_bot_id_inside_a_raw_text_table_cell(self):
        # Upstream also asserts the cell renders into ``text``; that is #210.
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "Import <@U_BOT>/passport",
                "ts": "1234567890.191919",
                "blocks": _raw_text_table(),
            }
        )
        assert message.is_mention is False

    async def test_does_not_flag_a_mention_token_in_a_rich_text_text_element(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "the app imports <@U_BOT>/passport",
                "ts": "1234567890.202021",
                "blocks": _rich_text(
                    {"type": "text", "text": "the app imports "},
                    {"type": "text", "text": "<@U_BOT>/passport"},
                ),
            }
        )
        assert message.is_mention is False

    async def test_does_not_flag_a_mention_token_in_a_link_label(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "<https://example.com|<@U_BOT>>",
                "ts": "1234567890.202022",
                "blocks": _rich_text({"type": "link", "url": "https://example.com", "text": "<@U_BOT>"}),
            }
        )
        assert message.is_mention is False

    async def test_flags_a_bot_mention_in_a_mrkdwn_section_block(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                # Only the section block carries the mention.
                "text": "",
                "ts": "1234567890.202023",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hey <@U_BOT> take a look"}}],
            }
        )
        assert message.is_mention is True

    async def test_does_not_flag_a_bot_id_in_a_plain_text_section_block(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "hey <@U_BOT> take a look",
                "ts": "1234567890.202024",
                "blocks": [{"type": "section", "text": {"type": "plain_text", "text": "hey <@U_BOT> take a look"}}],
            }
        )
        assert message.is_mention is False

    async def test_does_not_read_attachment_fallback_when_the_attachment_has_blocks(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "",
                "ts": "1234567890.202025",
                "attachments": [
                    {
                        "fallback": "<@U_BOT> is the bot",
                        "blocks": [
                            {
                                "type": "rich_text",
                                "elements": [
                                    {
                                        "type": "rich_text_preformatted",
                                        "elements": [{"type": "text", "text": "<@U_BOT>"}],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        assert message.is_mention is False

    async def test_does_not_flag_the_bots_display_name_inside_a_text_only_code_span(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "the docs say `@Test Bot`",
                "ts": "1234567890.202020",
            }
        )
        assert message.is_mention is False

    async def test_reports_false_for_an_ordinary_message_that_never_refers_to_the_bot(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "hey <@U_OTHER> can you look?",
                "ts": "1234567890.202022",
            }
        )
        assert message.is_mention is False

    async def test_trusts_app_mention_when_the_content_never_shows_the_known_bot_id(self):
        # Enterprise Grid emits the bot's W… id while auth.test reported U….
        message = await _parse_incoming(
            {
                "type": "app_mention",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "<@W_BOT_GRID> help me",
                "ts": "1234567890.202023",
                "blocks": _rich_text({"type": "user", "user_id": "W_BOT_GRID"}, {"type": "text", "text": " help me"}),
            }
        )
        assert message.is_mention is True

    async def test_trusts_app_mention_when_the_bot_id_is_unknown(self):
        client = _Client(default_name="Test Bot", auth={"ok": True})
        adapter = _make_adapter(client)
        chat = _make_mock_chat()
        await adapter.initialize(chat)
        await adapter.handle_webhook(
            _webhook(
                {
                    "type": "app_mention",
                    "user": "U_SENDER",
                    "channel": "C456",
                    "text": "<@U_BOT> help me",
                    "ts": "1234567890.222222",
                }
            )
        )
        message = await chat.process_message.call_args.args[2]()
        assert adapter.bot_user_id is None
        assert message.is_mention is True

    async def test_falls_back_to_the_bots_user_id_when_users_info_fails_for_its_own_mention(self):
        message = await _parse_incoming(
            {
                "type": "app_mention",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "<@U_BOT> help me",
                "ts": "1234567890.777777",
            },
            _Client(default_name="Test User", fail=frozenset({"U_BOT"})),
        )
        # The raw angle-bracket markup never resurfaces in the rendered text.
        assert message.text == "@U_BOT help me"
        assert message.is_mention is True

    async def test_resolves_request_scoped_self_mention_in_multi_workspace_mode(self):
        client = _Client(default_name="Workspace Bot")
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret=SECRET))
        adapter._get_client = lambda token=None: client  # type: ignore[assignment]
        await adapter.initialize(_make_mock_chat())
        token = adapter._request_context.set(RequestContext(token="xoxb-multi-token", bot_user_id="U_BOT_MULTI"))
        try:
            result = await adapter._resolve_inline_mentions("<@U_BOT_MULTI> help me")
        finally:
            adapter._request_context.reset(token)
        assert result == "<@U_BOT_MULTI|Workspace Bot> help me"


# ---------------------------------------------------------------------------
# DM / channel delivery
# ---------------------------------------------------------------------------


class TestDMMessageHandling:
    async def test_dm_messages_without_a_bot_mention_report_is_mention_false(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_USER",
                "channel": "D_DM_CHAN",
                "channel_type": "im",
                "text": "hello from DM",
                "ts": "1234567890.333333",
            }
        )
        assert message.is_mention is False

    async def test_uslack_system_notifications_in_dms_are_dispatched_with_is_system_set(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "USLACK",
                "channel": "D_DM_CHAN",
                "channel_type": "im",
                "text": "<@U_USER> archived the channel <#C_CHANNEL>",
                "ts": "1234567890.555555",
            },
            _Client(default_name="Slackbot"),
        )
        author = message.author
        assert (author.user_id, author.is_bot, author.is_system, author.is_me) == ("USLACK", False, True, False)

    async def test_channel_messages_without_a_bot_mention_report_is_mention_false(self):
        message = await _parse_incoming(
            {
                "type": "message",
                "user": "U_USER",
                "channel": "C_CHANNEL",
                "text": "hello from channel",
                "ts": "1234567890.444444",
            }
        )
        assert message.is_mention is False


# ---------------------------------------------------------------------------
# Mention routing through a real Chat
# ---------------------------------------------------------------------------


class TestMentionRouting:
    async def _routed_bot(self, bot_user_id: str | None = "U_BOT") -> tuple[SlackAdapter, Chat]:
        auth = {"ok": True, "user_id": bot_user_id, "bot_id": "B_BOT"} if bot_user_id else {"ok": True}
        client = _Client(default_name="Example Bot", auth=auth)
        overrides: dict[str, Any] = {"user_name": "Example Bot"}
        if bot_user_id:
            overrides["bot_user_id"] = bot_user_id
        adapter = _make_adapter(client, **overrides)
        bot = Chat(
            ChatConfig(
                user_name="Example Bot",
                adapters={"slack": adapter},
                state=create_memory_state(),
                logger=MagicMock(),
            )
        )
        await bot.initialize()
        return adapter, bot

    @staticmethod
    async def _deliver(adapter: SlackAdapter, event: dict[str, Any]) -> None:
        pending: list[Any] = []
        await adapter.handle_webhook(_webhook(event), WebhookOptions(wait_until=pending.append))
        await asyncio.gather(*pending)

    def _handlers(self, bot: Chat) -> tuple[AsyncMock, AsyncMock]:
        mention_handler = AsyncMock()
        message_handler = AsyncMock()
        bot.on_mention(mention_handler)
        bot.on_message(ANY_TEXT_PATTERN)(message_handler)
        return mention_handler, message_handler

    async def test_routes_a_code_only_reference_to_message_handlers_not_mention_handlers(self):
        adapter, bot = await self._routed_bot()
        mention_handler, message_handler = self._handlers(bot)
        # The bot's display name is in the flattened text, so the SDK's text
        # detection would otherwise route this to the mention handler.
        await self._deliver(
            adapter,
            {
                "type": "app_mention",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "the app imports `<@U_BOT>/passport`",
                "ts": "1234567890.202020",
                "blocks": _code_only_blocks(),
            },
        )
        mention_handler.assert_not_awaited()
        message_handler.assert_awaited_once()

    async def test_routes_a_real_mention_to_mention_handlers(self):
        adapter, bot = await self._routed_bot()
        mention_handler, message_handler = self._handlers(bot)
        await self._deliver(
            adapter,
            {
                "type": "app_mention",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "<@U_BOT> help me",
                "ts": "1234567890.212121",
                "blocks": _rich_text({"type": "user", "user_id": "U_BOT"}),
            },
        )
        mention_handler.assert_awaited_once()
        message_handler.assert_not_awaited()

    async def test_routes_a_code_only_reference_with_misleading_fallback_text_to_message_handlers(self):
        adapter, bot = await self._routed_bot()
        mention_handler, message_handler = self._handlers(bot)
        await self._deliver(
            adapter,
            {
                "type": "app_mention",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "the app imports <@U_BOT>/passport",
                "ts": "1234567890.222222",
                "blocks": _code_only_blocks(),
            },
        )
        mention_handler.assert_not_awaited()
        message_handler.assert_awaited_once()

    async def test_routes_a_raw_text_table_cell_reference_to_message_handlers(self):
        adapter, bot = await self._routed_bot()
        mention_handler, message_handler = self._handlers(bot)
        await self._deliver(
            adapter,
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "Import <@U_BOT>/passport",
                "ts": "1234567890.232323",
                "blocks": _raw_text_table(),
            },
        )
        mention_handler.assert_not_awaited()
        message_handler.assert_awaited_once()

    async def test_routes_a_text_only_code_reference_to_message_handlers(self):
        adapter, bot = await self._routed_bot()
        mention_handler, message_handler = self._handlers(bot)
        # No id markup at all: the configured name appears only in a code span.
        await self._deliver(
            adapter,
            {
                "type": "message",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "the docs say `@Example Bot`",
                "ts": "1234567890.242424",
            },
        )
        mention_handler.assert_not_awaited()
        message_handler.assert_awaited_once()

    async def test_trusts_app_mention_when_slack_reports_an_id_the_adapter_does_not_know(self):
        adapter, bot = await self._routed_bot()
        mention_handler, _ = self._handlers(bot)
        await self._deliver(
            adapter,
            {
                "type": "app_mention",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "<@W_BOT_GRID> help me",
                "ts": "1234567890.252524",
                "blocks": _rich_text({"type": "user", "user_id": "W_BOT_GRID"}, {"type": "text", "text": " help me"}),
            },
        )
        mention_handler.assert_awaited_once()

    async def test_trusts_app_mention_when_the_bot_id_is_unresolved(self):
        adapter, bot = await self._routed_bot(None)
        mention_handler, _ = self._handlers(bot)
        await self._deliver(
            adapter,
            {
                "type": "app_mention",
                "user": "U_SENDER",
                "channel": "C456",
                "text": "<@U_BOT> help me",
                "ts": "1234567890.252525",
            },
        )
        assert adapter.bot_user_id is None
        mention_handler.assert_awaited_once()


# ---------------------------------------------------------------------------
# Python-specific: mention classification edges
# ---------------------------------------------------------------------------


def _is_mention(text: str = "", **fields: Any) -> bool | None:
    """``is_mention`` of a plain channel message for bot ``U_BOT`` (sync path)."""
    adapter = _make_adapter(_Client(), bot_user_id="U_BOT")
    event: dict[str, Any] = {"type": "message", "user": "U123", "channel": "C456", "text": text, "ts": "1.1"}
    return adapter.parse_message({**event, **fields}).is_mention


class TestMentionMatcher:
    """``_MentionMatcher.search`` replaces upstream's ``/<@!?id(?:\\|[^>]*)?>/i``
    with a prefix match plus a tail check; these pin it to the regex."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("<@U_BOT> hi", True),
            ("<@U_BOT|bot> hi", True),
            ("<@!U_BOT> hi", True),
            ("<@u_bot> hi", True),
            # A different user whose id starts with the bot's id.
            ("<@U_BOT2> hi", False),
            ("<@U_BOT hi", False),
            ("<@U_BOT|bot hi", False),
            # The only ``>`` comes before the ``|`` form, so nothing closes it.
            ("a > b <@U_BOT|bot", False),
        ],
    )
    def test_token_forms(self, text: str, expected: bool):
        assert _is_mention(text) is expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("<@U_BOT|" * 6000 + ">", True),
            ("<@U_BOT|" * 6000, False),
            ("<" * 50000 + "@U_BOT>", True),
            ("<@U_BOT> " + "`" * 50000, True),
            ("`<@U_BOT>" + "`x" * 25000, False),
            ("```" * 16000 + "<@U_BOT>", True),
        ],
        ids=[
            "unclosed-pipe-run-closed",
            "unclosed-pipe-run",
            "angle-run",
            "backtick-run",
            "alternating-ticks",
            "fences",
        ],
    )
    def test_classifies_50k_char_adversarial_inputs(self, text: str, expected: bool):
        """Each input is about 50k chars; the expected value is what upstream's
        regex (plus code masking) yields for it. Calls the classifier directly:
        rendering the text (``to_ast``) is not what is under test here."""
        adapter = _make_adapter(_Client(), bot_user_id="U_BOT")
        event = {"type": "message", "user": "U123", "channel": "C456", "text": text, "ts": "1.1"}
        assert adapter._detect_self_mention(event, text, []) is expected

    def test_classifies_deeply_nested_blocks_without_recursion(self):
        node: dict[str, Any] = {"type": "user", "user_id": "U_BOT"}
        for _ in range(5000):
            node = {"type": "rich_text_section", "elements": [node]}
        assert _is_mention("", blocks=[{"type": "rich_text", "elements": [node]}]) is True


class TestBlockMentionRules:
    def test_does_not_flag_a_code_span_inside_a_mrkdwn_section(self):
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "x `<@U_BOT>`"}}]
        assert _is_mention("x <@U_BOT>", blocks=blocks) is False

    def test_flags_a_mention_in_section_fields(self):
        blocks = [{"type": "section", "fields": [{"type": "mrkdwn", "text": "hi <@U_BOT>"}]}]
        assert _is_mention("", blocks=blocks) is True

    def test_does_not_flag_a_user_element_inside_a_preformatted_block(self):
        """The ``user`` element inherits code status from its container."""
        blocks = [
            {
                "type": "rich_text",
                "elements": [{"type": "rich_text_preformatted", "elements": [{"type": "user", "user_id": "U_BOT"}]}],
            }
        ]
        assert _is_mention("<@U_BOT>", blocks=blocks) is False


class TestAttachmentPartRules:
    @pytest.mark.parametrize(
        ("attachment", "expected"),
        [
            ({"pretext": "hey <@U_BOT>"}, True),
            # ``mrkdwn_in`` makes backticks code; without it they are literal.
            ({"pretext": "`<@U_BOT>`", "mrkdwn_in": ["pretext"]}, False),
            ({"pretext": "`<@U_BOT>`"}, True),
            ({"title": "<@U_BOT>"}, True),
            # The title is escaped inside the link, so its token is not a mention.
            ({"title": "<@U_BOT>", "title_link": "https://example.com"}, False),
            ({"fields": [{"title": "Owner", "value": "<@U_BOT>"}]}, True),
            ({"fields": [{"value": "`<@U_BOT>`"}], "mrkdwn_in": ["fields"]}, False),
            ({"fallback": "hi <@U_BOT>"}, True),
            # ``fallback`` fills in only when nothing else renders.
            ({"text": "hi", "fallback": "hi <@U_BOT>"}, False),
            # A whitespace-only field renders nothing, so ``fallback`` fills in.
            ({"pretext": "   ", "fallback": "hi <@U_BOT>"}, True),
        ],
    )
    def test_classifies_legacy_attachment_parts(self, attachment: dict[str, Any], expected: bool):
        assert _is_mention("", attachments=[attachment]) is expected

    def test_builds_legacy_parts_beside_blocks_without_tables(self):
        """Upstream gates the parts on ``tables.length === 0``, not on blocks."""
        content = _attachment_content({"blocks": [{"type": "divider"}], "fallback": "hi"})
        assert content.parts == [_AttachmentPart(text="hi", mrkdwn=False)]


# ---------------------------------------------------------------------------
# Author identity
# ---------------------------------------------------------------------------


class TestIsMessageFromSelf:
    def test_matches_by_bot_profile_user_id(self):
        adapter = _make_adapter(_Client())
        adapter._bot_user_id = "U_BOT_123"
        assert adapter._is_message_from_self({"bot_id": "B_BOT_456", "bot_profile": {"user_id": "U_BOT_123"}}) is True

    def test_matches_request_bot_user_id_by_bot_profile(self):
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s"))
        token = adapter._request_context.set(RequestContext(token="xoxb-test", bot_user_id="U_BOT_123"))
        try:
            result = adapter._is_message_from_self({"bot_id": "B_BOT_456", "bot_profile": {"user_id": "U_BOT_123"}})
        finally:
            adapter._request_context.reset(token)
        assert result is True


class TestSystemAuthoredMessages:
    async def test_marks_messages_from_uslack_as_is_system(self):
        adapter = _make_adapter(_Client(default_name="Slackbot"))
        await adapter.initialize(_make_mock_chat())
        message = await adapter._parse_slack_message(
            {
                "type": "message",
                "user": "USLACK",
                "channel_type": "im",
                "text": "<@U123> archived the channel <#C123>",
                "ts": "1234567890.123456",
                "channel": "D123",
            },
            "slack:D123:1234567890.123456",
        )
        author = message.author
        assert (author.user_id, author.is_bot, author.is_system, author.is_me) == ("USLACK", False, True, False)

    async def test_marks_human_authored_messages_as_not_is_system(self):
        adapter = _make_adapter(_Client())
        await adapter.initialize(_make_mock_chat())
        message = await adapter._parse_slack_message(
            {
                "type": "message",
                "user": "U_HUMAN_1",
                "username": "human",
                "text": "Hello",
                "ts": "1234567890.123456",
                "channel": "C123",
            },
            "slack:C123:1234567890.123456",
        )
        author = message.author
        assert (author.user_id, author.is_bot, author.is_system, author.is_me) == ("U_HUMAN_1", False, False, False)


class TestIncomingAuthorEmail:
    HUMAN_EVENT: dict[str, Any] = {
        "type": "message",
        "user": "U_HUMAN_1",
        "text": "Hello",
        "ts": "1234567890.123456",
        "channel": "C123",
    }

    async def _adapter(self, client: _Client) -> SlackAdapter:
        adapter = _make_adapter(client, bot_user_id="U_BOT")
        chat = _make_mock_chat()
        # A real in-memory cache so the second parse can be served from it.
        state = create_memory_state()
        await state.connect()
        chat.get_state = MagicMock(return_value=state)
        await adapter.initialize(chat)
        return adapter

    async def test_hydrates_author_email_from_users_info(self):
        adapter = await self._adapter(_Client({"U_HUMAN_1": "Alice"}, emails={"U_HUMAN_1": "alice@example.com"}))
        message = await adapter._parse_slack_message(dict(self.HUMAN_EVENT), "slack:C123:1234567890.123456")
        assert message.author.email == "alice@example.com"

    async def test_leaves_email_undefined_when_the_profile_has_none(self):
        adapter = await self._adapter(_Client({"U_HUMAN_1": "Alice"}))
        message = await adapter._parse_slack_message(dict(self.HUMAN_EVENT), "slack:C123:1234567890.123456")
        assert message.author.email is None

    async def test_leaves_email_undefined_when_the_user_lookup_is_skipped(self):
        client = _Client(emails={"U_HUMAN_1": "alice@example.com"})
        adapter = await self._adapter(client)
        event = {**self.HUMAN_EVENT, "username": "webhook-bot"}
        del event["user"]
        message = await adapter._parse_slack_message(event, "slack:C123:1234567890.123456")
        client.users_info.assert_not_awaited()
        assert message.author.email is None

    async def test_serves_email_from_the_user_cache_without_a_second_users_info_call(self):
        client = _Client({"U_HUMAN_1": "Alice"}, emails={"U_HUMAN_1": "alice@example.com"})
        adapter = await self._adapter(client)
        await adapter._parse_slack_message(dict(self.HUMAN_EVENT), "slack:C123:1234567890.123456")
        second = await adapter._parse_slack_message(dict(self.HUMAN_EVENT), "slack:C123:1234567890.123456")
        assert client.users_info.await_count == 1
        assert second.author.email == "alice@example.com"


# ---------------------------------------------------------------------------
# post_channel_message
# ---------------------------------------------------------------------------


class TestPostChannelMessage:
    async def test_posts_to_channel_without_thread_context(self):
        client = _Client()
        adapter = _make_adapter(client)
        result = await adapter.post_channel_message("slack:C123", "Top-level message")
        assert result.id == "2222222222.000000"
        assert result.thread_id == "slack:C123:2222222222.000000"
        kwargs = client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "C123"
        assert kwargs.get("thread_ts") is None

    async def test_keeps_the_synthetic_thread_id_when_the_response_has_no_string_ts(self):
        """Python addition: without a string ``ts`` there is no thread to
        address, so the synthetic ``slack:C123:`` id is kept."""
        client = _Client()
        client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": 2222222222})
        adapter = _make_adapter(client)
        result = await adapter.post_channel_message("slack:C123", "Top-level message")
        assert result.thread_id == "slack:C123:"
