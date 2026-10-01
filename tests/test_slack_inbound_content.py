"""Slack inbound content: pasted tables and alert attachments.

Port of the ``packages/adapter-slack/src/index.test.ts`` cases added by
upstream vercel/chat#817 (764e4759, chat@4.38.1) and #846 (864d9222,
chat@4.39.0), plus Python-specific guards. ``index.test.ts`` is not
fidelity-mapped; test names follow the upstream ``it(...)`` titles.
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

from chat_sdk.adapters.slack.adapter import (
    SlackAdapter,
    _apply_mention_names,
    _block_text,
    _literal_phrasing,
    _MentionNames,
)
from chat_sdk.adapters.slack.types import SlackAdapterConfig
from chat_sdk.chat import Chat
from chat_sdk.state.memory import create_memory_state
from chat_sdk.types import ChatConfig, Message, WebhookOptions

SECRET = "test-signing-secret"
THREAD_ID = "slack:C456:1786120899.208429"

# The ``Ev0ATABLE001`` webhook from upstream ``packages/adapter-slack/sample-messages.md``.
TABLE_WEBHOOK_BODY = (
    '{"token":"xAbCdEfGhIjKlMnOpQrStUvW","team_id":"T00FAKE00AA","context_team_id":"T00FAKE00AA",'
    '"context_enterprise_id":null,"api_app_id":"A00FAKEAPP01","event":{"type":"message",'
    '"user":"U00FAKEUSER1","ts":"1786120899.208429","client_msg_id":"8f1c2d3e-45a6-47b8-9c0d-1e2f3a4b5c6d",'
    '"text":"Which devices support remote firmware upgrades?","team":"T00FAKE00AA","blocks":[{"type":'
    '"rich_text","block_id":"tblQ1","elements":[{"type":"rich_text_section","elements":[{"type":"text",'
    '"text":"Which devices support remote firmware upgrades?"}]}]}],"attachments":[{"id":1,"fallback":'
    '"[no preview available]","blocks":[{"type":"table","block_id":"pasted1","rows":[[{"type":"rich_text",'
    '"elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"Manufacturer","style":'
    '{"bold":true}}]}]},{"type":"raw_text","text":"Identifier Listed"},{"type":"raw_text","text":"Units"}],'
    '[{"type":"raw_text","text":"Samsung"},{"type":"raw_text","text":"QB55C"},{"type":"raw_number",'
    '"value":3}]]}]}],"channel":"C00FAKECHAN1","event_ts":"1786120899.208429","channel_type":"channel"},'
    '"type":"event_callback","event_id":"Ev0ATABLE001","event_time":1786120899,"authorizations":'
    '[{"enterprise_id":null,"team_id":"T00FAKE00AA","user_id":"U00FAKEBOT01","is_bot":true,'
    '"is_enterprise_install":false}],"is_ext_shared_channel":false}'
)
TABLE_TEXT = (
    "Which devices support remote firmware upgrades?\n\nManufacturer\tIdentifier Listed\tUnits\nSamsung\tQB55C\t3"
)


@pytest.fixture(autouse=True)
def _no_unfurl_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Messages with links poll the unfurl cache; nothing here caches one."""
    monkeypatch.setattr("chat_sdk.adapters.slack.adapter._UNFURL_WAIT_MS", 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(names: dict[str, str] | None = None) -> MagicMock:
    """Slack Web API stand-in whose ``users.info`` answers from *names*."""
    names = names or {}

    async def users_info(*, user: str) -> dict[str, Any]:
        name = names.get(user, "User")
        return {"ok": True, "user": {"name": name, "real_name": name, "profile": {"display_name": name}}}

    client = MagicMock()
    client.users_info = AsyncMock(side_effect=users_info)
    client.conversations_info = AsyncMock(return_value={"ok": True, "channel": {"name": "general"}})
    client.auth_test = AsyncMock(return_value={"ok": True, "user_id": "U_BOT", "bot_id": "B_BOT"})
    return client


def _adapter(client: MagicMock | None = None) -> SlackAdapter:
    adapter = SlackAdapter(SlackAdapterConfig(signing_secret=SECRET, bot_token="xoxb-test-token", bot_user_id="U_BOT"))
    resolved = client if client is not None else _client()
    adapter._get_client = lambda token=None: resolved  # type: ignore[assignment]
    return adapter


def _parse(**event: Any) -> Message:
    """Sync ``parse_message`` of a channel message from ``U123``."""
    base: dict[str, Any] = {"type": "message", "user": "U123", "channel": "C456", "ts": "1786120899.208429"}
    return _adapter().parse_message({**base, **event})


def _raw(text: str) -> dict[str, Any]:
    return {"type": "raw_text", "text": text}


def _table(*rows: list[dict[str, Any]], block_type: str = "table") -> dict[str, Any]:
    return {"type": block_type, "rows": list(rows)}


def _signed(body: str) -> Any:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    request = MagicMock()
    request.body = body.encode("utf-8")
    request.headers = {"x-slack-request-timestamp": ts, "x-slack-signature": sig, "content-type": "application/json"}
    request.url = ""
    request.text = AsyncMock(return_value=body)
    return request


def _rich_cell(*elements: dict[str, Any], section: str = "rich_text_section") -> dict[str, Any]:
    return {"type": "rich_text", "elements": [{"type": section, "elements": list(elements)}]}


def _cell_values(row: dict[str, Any]) -> list[list[Any]]:
    return [[child.get("value") for child in cell["children"]] for cell in row["children"]]


# ---------------------------------------------------------------------------
# Pasted tables (upstream #817)
# ---------------------------------------------------------------------------


class TestPastedTables:
    async def test_preserves_pasted_table_attachments_as_message_content(self):
        event = {**json.loads(TABLE_WEBHOOK_BODY)["event"], "username": "alice"}
        adapter = _adapter()
        sync = adapter.parse_message(event)
        parsed = await adapter._parse_slack_message(event, THREAD_ID)

        for message in (sync, parsed):
            assert message.text == TABLE_TEXT
            assert message.attachments == []
            table = message.formatted["children"][1]
            assert table["type"] == "table"
            # The bold first row is the header: no placeholder row is added.
            assert [_cell_values(row) for row in table["children"]] == [
                [["Manufacturer"], ["Identifier Listed"], ["Units"]],
                [["Samsung"], ["QB55C"], ["3"]],
            ]

    def test_preserves_table_only_messages_and_ignores_malformed_table_blocks(self):
        message = _parse(
            text="",
            blocks=[_table([_raw("Visible")]), {"type": "table", "rows": "invalid"}],
            attachments=[{"blocks": [{"type": "table"}]}],
        )
        assert message.text == "Visible"
        assert [child["type"] for child in message.formatted["children"]] == ["table"]

    def test_preserves_inline_rich_text_within_table_cells(self):
        cell = _rich_cell(
            {"type": "text", "text": "See "},
            {"type": "link", "text": "details", "url": "https://example.com"},
            section="rich_text_quote",
        )
        message = _parse(text="", blocks=[_table([cell])])

        assert message.text == "See details"
        # Headerless table: an empty header row is inserted so GFM doesn't
        # promote the data row to a header. The link survives as a link node.
        assert message.formatted["children"][0] == {
            "type": "table",
            "children": [
                {"type": "tableRow", "children": [{"type": "tableCell", "children": []}]},
                {
                    "type": "tableRow",
                    "children": [
                        {
                            "type": "tableCell",
                            "children": [
                                {"type": "text", "value": "See "},
                                {
                                    "type": "link",
                                    "url": "https://example.com",
                                    "children": [{"type": "text", "value": "details"}],
                                },
                            ],
                        }
                    ],
                },
            ],
        }

    def test_preserves_rich_text_metadata_within_table_cells(self):
        cell = _rich_cell(
            {"type": "channel", "channel_id": "C789"},
            {"type": "text", "text": " "},
            {"type": "usergroup", "usergroup_id": "S789"},
            {"type": "text", "text": " "},
            {"type": "date", "timestamp": 1_720_710_212, "format": "{date_num}", "fallback": "July 11"},
            {"type": "text", "text": " "},
            {"type": "color", "value": "#ff0000"},
        )
        # Cell tokens are emitted as mrkdwn for the converter that renders body text.
        assert _block_text(cell) == "<#C789> <!subteam^S789> July 11 #ff0000"
        message = _parse(text="", blocks=[_table([cell])])
        # Upstream expects ``@S789``: rendering ``<!subteam^…>`` belongs to the
        # mrkdwn converter (``convertSpecialMentions``), ported by #283.
        assert message.text == "#C789 <!subteam^S789> July 11 #ff0000"

    def test_formats_date_cells_from_the_timestamp_when_no_fallback_is_present(self):
        message = _parse(
            text="", blocks=[_table([{"type": "date", "timestamp": 1_720_710_212, "format": "{date_num}"}])]
        )
        assert message.text == "2024-07-11"

    def test_preserves_empty_and_raw_value_cells_so_columns_stay_aligned(self):
        message = _parse(
            text="",
            blocks=[
                _table(
                    [_raw("Samsung"), _raw(""), {"type": "raw_number", "value": 3}],
                    [_raw("LG"), {"type": "raw_boolean", "value": True}, {"type": "raw_number", "value": "7"}],
                )
            ],
        )
        assert message.text == "Samsung\t\t3\nLG\ttrue\t7"

    def test_parses_data_table_blocks_with_their_header_row_intact(self):
        block = {**_table([_raw("Manufacturer"), _raw("Units")], [_raw("Samsung"), _raw("3")], block_type="data_table")}
        block["caption"] = "Devices"
        message = _parse(text="", blocks=[block])

        assert message.text == "Manufacturer\tUnits\nSamsung\t3"
        # data_table rows always start with a header row; no placeholder is added
        table = message.formatted["children"][0]
        assert [_cell_values(row) for row in table["children"]] == [
            [["Manufacturer"], ["Units"]],
            [["Samsung"], ["3"]],
        ]

    def test_ignores_tables_in_unfurl_and_app_attachments(self):
        block = _table([_raw("Foreign")])
        message = _parse(
            text="Check this out",
            attachments=[
                {"is_msg_unfurl": True, "blocks": [block]},
                {"is_app_unfurl": True, "blocks": [block]},
                {"from_url": "https://example.com", "blocks": [block]},
                {"original_url": "https://example.com/page", "blocks": [block]},
            ],
        )
        assert message.text == "Check this out"
        assert len(message.formatted["children"]) == 1

    def test_keeps_tables_pasted_above_the_message_text_above_it(self):
        message = _parse(
            text="The table above shows Q1",
            blocks=[_table([_raw("Row")]), _rich_cell({"type": "text", "text": "The table above shows Q1"})],
        )
        assert [child["type"] for child in message.formatted["children"]] == ["table", "paragraph"]
        assert message.text == "Row\n\nThe table above shows Q1"

    def test_does_not_leave_raw_bot_mention_tokens_in_table_cells(self):
        message = _parse(text="", blocks=[_table([_raw("On call"), {"type": "user", "user_id": "UBOT123"}])])
        # A raw <@UBOT123> would false-positive the core's text mention
        # fallback; the converter rewrites it like body text.
        assert message.text == "On call\t@UBOT123"


# ---------------------------------------------------------------------------
# Alert attachments (upstream #846)
# ---------------------------------------------------------------------------


class TestAlertAttachments:
    async def test_preserves_alert_attachment_content_as_message_content(self):
        event = {
            "type": "message",
            "user": "U123",
            "username": "sentry",
            "channel": "C456",
            "text": "New alert",
            "ts": "1786120899.208429",
            "attachments": [
                {
                    "fallback": "[Sentry] TypeError in checkout",
                    "title": "TypeError: cannot read property 'id' of undefined",
                    "text": "Occurred 42 times in the last hour.",
                    "fields": [
                        {"title": "Project", "value": "storefront", "short": True},
                        {"title": "Environment", "value": "production", "short": True},
                    ],
                }
            ],
        }
        expected = (
            "New alert\n\n"
            "TypeError: cannot read property 'id' of undefined\n"
            "Occurred 42 times in the last hour.\n"
            "Project: storefront\n"
            "Environment: production"
        )
        adapter = _adapter()
        sync = adapter.parse_message(event)
        parsed = await adapter._parse_slack_message(event, THREAD_ID)
        assert sync.text == expected
        assert parsed.text == expected

    def test_falls_back_to_attachment_fallback_only_when_nothing_else_carries_content(self):
        message = _parse(text="Deploy finished", attachments=[{"fallback": "build #421 succeeded"}])
        assert message.text == "Deploy finished\n\nbuild #421 succeeded"

    def test_ignores_content_in_unfurl_and_app_attachments(self):
        message = _parse(
            text="Check this out",
            attachments=[
                {"is_msg_unfurl": True, "title": "Foreign title", "text": "Foreign text"},
                {"is_app_unfurl": True, "fallback": "Foreign fallback"},
                {"from_url": "https://example.com", "title": "Preview"},
                {"original_url": "https://example.com/page", "fields": [{"title": "Key", "value": "Value"}]},
            ],
        )
        assert message.text == "Check this out"

    def test_keeps_attachment_formatting_characters_literal_unless_mrkdwn_in_enables_them(self):
        attachment = {"title": "Cleanup failed in <module>", "text": "rm -rf /tmp/*cache* failed for _id_ values"}
        literal = _parse(text="Alert", attachments=[dict(attachment)])
        # Slack renders attachment text as plain text unless mrkdwn_in lists it
        assert literal.text == "Alert\n\nCleanup failed in <module>\nrm -rf /tmp/*cache* failed for _id_ values"

        mrkdwn = _parse(
            text="Alert",
            attachments=[{**attachment, "mrkdwn_in": ["text"], "text": "deploy *failed* badly"}],
        )
        # With mrkdwn_in, *bold* is markup; the title stays plain text
        assert mrkdwn.text == "Alert\n\nCleanup failed in <module>\n\ndeploy failed badly"
        assert mrkdwn.formatted["children"][2] == {
            "type": "paragraph",
            "children": [
                {"type": "text", "value": "deploy "},
                {"type": "strong", "children": [{"type": "text", "value": "failed"}]},
                {"type": "text", "value": " badly"},
            ],
        }

    def test_keeps_attachment_content_out_of_an_unclosed_code_fence_in_the_body(self):
        message = _parse(
            text="Deploy failed:\n```\nTypeError: boom",
            attachments=[{"title": "Deploy status", "fields": [{"title": "Environment", "value": "production"}]}],
        )
        # The unclosed fence swallows the rest of the body, but the
        # attachment parses in isolation and stays a paragraph.
        assert [child["type"] for child in message.formatted["children"]] == ["paragraph", "code", "paragraph"]
        assert message.text == "Deploy failed:\n\nTypeError: boom\n\nDeploy status\nEnvironment: production"

    def test_uses_the_fallback_when_attachment_blocks_carry_nothing_renderable(self):
        message = _parse(
            text="Heads up",
            attachments=[
                {
                    "fallback": "Deploy failed on step 3",
                    "blocks": [{"type": "section", "text": "Deploy failed on step 3"}],
                }
            ],
        )
        assert message.text == "Heads up\n\nDeploy failed on step 3"

    def test_prefers_attachment_blocks_over_legacy_fields_matching_slack_rendering(self):
        message = _parse(
            text="Report",
            attachments=[
                {
                    "fallback": "table fallback",
                    "title": "Legacy title Slack does not render",
                    "blocks": [_table([_raw("Region"), _raw("Status")], [_raw("us-east"), _raw("down")])],
                }
            ],
        )
        assert message.text == "Report\n\nRegion\tStatus\nus-east\tdown"

    def test_links_the_attachment_title_to_title_link_and_surfaces_the_url(self):
        message = _parse(
            text="New issue",
            attachments=[{"title": "TypeError in checkout", "title_link": "https://sentry.example.com/issues/123"}],
        )
        assert message.text == "New issue\n\nTypeError in checkout"
        assert message.formatted["children"][1] == {
            "type": "paragraph",
            "children": [
                {
                    "type": "link",
                    "url": "https://sentry.example.com/issues/123",
                    "children": [{"type": "text", "value": "TypeError in checkout"}],
                }
            ],
        }
        assert [link.url for link in message.links] == ["https://sentry.example.com/issues/123"]

    def test_keeps_each_attachments_tables_adjacent_to_its_text(self):
        message = _parse(
            text="Two alerts",
            attachments=[
                # Blocks win over legacy fields per attachment, so give each
                # attachment either text or a table and check the interleaving.
                {"title": "Alert A"},
                {"blocks": [_table([_raw("table A")])]},
                {"title": "Alert B"},
                {"blocks": [_table([_raw("table B")])]},
            ],
        )
        assert message.text == "Two alerts\n\nAlert A\n\ntable A\n\nAlert B\n\ntable B"

    async def test_resolves_mentions_in_attachment_content_with_a_single_lookup_per_user(self):
        client = _client({"U777": "jane"})
        adapter = _adapter(client)
        message = await adapter._parse_slack_message(
            {
                "type": "message",
                "user": "U123",
                "username": "pager",
                "channel": "C456",
                "text": "Incident",
                "ts": "1786120899.208429",
                "attachments": [
                    {"fields": [{"title": "Primary", "value": "<@U777>"}, {"title": "Secondary", "value": "<@U777>"}]}
                ],
            },
            THREAD_ID,
        )
        assert message.text == "Incident\n\nPrimary: @jane\nSecondary: @jane"
        assert client.users_info.await_count == 1

    async def test_resolves_user_and_channel_mentions_in_table_cells(self):
        """Upstream's "flags the bot's own mention in rich text table cells" text half."""
        client = _client({"U_BOT": "Test Bot"})
        adapter = _adapter(client)
        cells = [
            _rich_cell({"type": "user", "user_id": "U_BOT"}),
            _rich_cell({"type": "channel", "channel_id": "C789"}),
        ]
        event = {
            "type": "message",
            "user": "U123",
            "username": "alice",
            "channel": "C456",
            "text": "",
            "ts": "1786120899.208429",
            "blocks": [_table(cells)],
        }
        message = await adapter._parse_slack_message(event, THREAD_ID)
        assert message.text == "@Test Bot\t#general"
        client.users_info.assert_awaited_once_with(user="U_BOT")


# ---------------------------------------------------------------------------
# Python-specific guards
# ---------------------------------------------------------------------------


class TestPythonContentGuards:
    def test_falsy_raw_values_render_like_js_string(self):
        """``0`` / ``False`` / ``3.0`` are values: no truthiness, and JS ``String()`` formatting."""
        message = _parse(
            text="",
            blocks=[
                _table(
                    [
                        {"type": "raw_boolean", "value": False},
                        {"type": "raw_number", "value": 0},
                        {"type": "raw_number", "value": 3.0},
                        {"type": "raw_number", "value": 1.5},
                    ]
                )
            ],
        )
        assert message.text == "false\t0\t3\t1.5"

    def test_skips_non_dict_attachments_and_rows_without_raising(self):
        message = _parse(
            text="Body",
            attachments=["not an attachment", None, {"blocks": [_table("bad row", [], [_raw("ok")])]}],
        )
        assert message.text == "Body\n\nok"

    def test_out_of_range_date_cells_render_empty(self):
        """``toISOString`` throws upstream; ``fromtimestamp`` raises here. Keep the message."""
        message = _parse(text="Dates", blocks=[_table([_raw("a"), {"type": "date", "timestamp": 10**20}])])
        assert message.text == "a\t\n\nDates"

    def test_deeply_nested_cells_do_not_raise_recursion_error(self):
        cell: dict[str, Any] = {"type": "text", "text": "deep"}
        for _ in range(5000):
            cell = {"type": "rich_text_section", "elements": [cell]}
        assert _block_text(cell) == "deep"

    async def test_sync_and_async_paths_give_equal_content_without_mentions(self):
        event = {
            "type": "message",
            "user": "U123",
            "username": "alice",
            "channel": "C456",
            "text": "Body *bold*",
            "ts": "1786120899.208429",
            "blocks": [_table([_raw("lead")]), _rich_cell({"type": "text", "text": "Body"}), _table([_raw("tail")])],
            "attachments": [
                {"pretext": "pre", "text": "line one\n\nline two", "mrkdwn_in": ["pretext"]},
                {"blocks": [_table([_raw("x"), {"type": "raw_number", "value": 2}])]},
            ],
        }
        adapter = _adapter()
        sync = adapter.parse_message(event)
        parsed = await adapter._parse_slack_message(event, THREAD_ID)
        assert parsed.formatted == sync.formatted
        assert parsed.text == "lead\n\nBody bold\n\ntail\n\npre\n\nline one\n\nline two\n\nx\t2"

    def test_does_not_surface_the_title_link_of_an_unfurl(self):
        message = _parse(
            text="See",
            attachments=[
                {"is_app_unfurl": True, "title": "App", "title_link": "https://app.example.com/a"},
                {"title": "Own", "title_link": "https://sentry.example.com/1"},
            ],
        )
        assert [link.url for link in message.links] == ["https://sentry.example.com/1"]

    async def test_table_webhook_fixture_reaches_message_text(self):
        """The ``Ev0ATABLE001`` sample webhook end to end through ``handle_webhook``."""
        adapter = _adapter()
        chat = MagicMock()
        state = MagicMock()
        state.get = AsyncMock(return_value=None)
        state.set = AsyncMock()
        state.get_list = AsyncMock(return_value=[])
        state.append_to_list = AsyncMock()
        chat.get_state = MagicMock(return_value=state)
        chat.process_message = MagicMock()
        await adapter.initialize(chat)

        await adapter.handle_webhook(_signed(TABLE_WEBHOOK_BODY))
        factory = chat.process_message.call_args.args[2]
        message = await factory()
        assert message.text == TABLE_TEXT

    async def test_on_message_pattern_matches_attachment_only_text(self):
        """Routing change: a pattern that only attachment content matches now fires."""
        adapter = _adapter()
        bot = Chat(
            ChatConfig(
                user_name="Example Bot", adapters={"slack": adapter}, state=create_memory_state(), logger=MagicMock()
            )
        )
        await bot.initialize()
        handler = AsyncMock()
        bot.on_message(re.compile(r"storefront"))(handler)

        event = {
            "type": "message",
            "user": "U_ALERTS",
            "channel": "C456",
            "text": "New alert",
            "ts": "1786120899.555555",
            "attachments": [{"title": "TypeError", "fields": [{"title": "Project", "value": "storefront"}]}],
        }
        body = json.dumps({"type": "event_callback", "team_id": "T123", "event": event})
        pending: list[Any] = []
        await adapter.handle_webhook(_signed(body), WebhookOptions(wait_until=pending.append))
        await asyncio.gather(*pending)

        handler.assert_awaited_once()
        message = handler.await_args.args[1]
        assert message.text == "New alert\n\nTypeError\nProject: storefront"


class TestMentionAndLiteralScanners:
    NAMES = _MentionNames(users={"U1": "jane"}, channels={"C2": "general"})

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("<@U1> and <#C2>", "<@U1|jane> and <#C2|general>"),
            ("<#C2> and <@U1>", "<#C2|general> and <@U1|jane>"),
            # A label is replaced for users, kept for channels.
            ("<@U1|old> <#C2|kept>", "<@U1|jane> <#C2|kept>"),
            # Unknown ids and non-id tokens stay as written.
            ("<@U9> <#C9> <@lower> <!here>", "<@U9> <#C9> <@lower> <!here>"),
            # A token runs to the next ``>``, so ``<@U1 and <#C2>`` is one
            # (non-id) token and stays as written.
            ("<@U1> then <@U1 and <#C2>", "<@U1|jane> then <@U1 and <#C2>"),
            # An unclosed token ends the scan; the rest is kept verbatim.
            ("<#C2> <@U1", "<#C2|general> <@U1"),
            ("", ""),
        ],
    )
    def test_apply_mention_names(self, text: str, expected: str):
        assert _apply_mention_names(text, self.NAMES) == expected

    def test_literal_phrasing_honors_only_control_sequences(self):
        assert _literal_phrasing("*a* <@U1|jane> <#C2|gen> <#C3> &lt;b&gt; <https://x.io|X &amp; Y> <x") == [
            {"type": "text", "value": "*a* @jane #gen (C2) #C3 <b> "},
            {"type": "link", "url": "https://x.io", "children": [{"type": "text", "value": "X & Y"}]},
            {"type": "text", "value": " <x"},
        ]

    def test_scanners_stay_linear_on_long_token_runs(self):
        """Upstream re-slices per token; with Python's copying slices that is quadratic."""
        start = time.perf_counter()
        resolved = _apply_mention_names("<@U1>" * 40_000, self.NAMES)
        phrasing = _literal_phrasing("<x>" * 60_000)
        elapsed = time.perf_counter() - start
        assert resolved == "<@U1|jane>" * 40_000
        assert phrasing == [{"type": "text", "value": "<x>" * 60_000}]
        # The quadratic versions take several seconds on these inputs.
        assert elapsed < 1.0
