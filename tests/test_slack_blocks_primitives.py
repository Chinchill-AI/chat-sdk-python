"""Tests for the runtime-free Slack Block Kit primitives subpath.

Port of ``packages/adapter-slack/src/blocks/index.test.ts`` and
``blocks/boundary.test.ts`` (vercel/chat#555, #559), exposed upstream as
``@chat-adapter/slack/blocks``. Card-input keys are snake_case
(``image_url``, ``initial_option``, ``request_id``, ...) per the Python
port convention; emitted Block Kit dicts keep Slack's API field names.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from chat_sdk.adapters.slack.blocks import (
    LIMITS,
    answered_slack_input_blocks,
    build_slack_freeform_view,
    card_to_block_kit,
    card_to_fallback_text,
    card_to_slack_blocks,
    card_to_slack_fallback_text,
    convert_slack_emoji_placeholders,
    input_request_to_slack_blocks,
    parse_slack_freeform_value,
    parse_slack_input_response,
)


def _card(children: list[Any] | None = None) -> dict[str, Any]:
    return {"children": children if children is not None else [], "type": "card"}


class TestSlackBlockKitPrimitives:
    def test_converts_card_headers_and_context(self) -> None:
        assert card_to_slack_blocks(
            {
                "children": [],
                "image_url": "https://example.com/image.png",
                "subtitle": "Status changed",
                "title": "Order",
                "type": "card",
            }
        ) == [
            {
                "text": {"emoji": True, "text": "Order", "type": "plain_text"},
                "type": "header",
            },
            {
                "elements": [{"text": "Status changed", "type": "mrkdwn"}],
                "type": "context",
            },
            {
                "alt_text": "Order",
                "image_url": "https://example.com/image.png",
                "type": "image",
            },
        ]

    def test_truncates_header_text_to_header_block_limit(self) -> None:
        title = "a" * 200

        assert card_to_slack_blocks({"children": [], "title": title, "type": "card"})[0] == {
            "text": {"emoji": True, "text": "a" * 150, "type": "plain_text"},
            "type": "header",
        }

    def test_truncates_image_urls_to_image_block_limit(self) -> None:
        long_url = "https://example.com/" + "a" * 4000
        top_blocks = card_to_slack_blocks(
            {
                "children": [],
                "image_url": long_url,
                "title": "{{emoji:frame}}",
                "type": "card",
            }
        )

        assert top_blocks[0] == {
            "text": {"emoji": True, "text": ":frame:", "type": "plain_text"},
            "type": "header",
        }
        assert top_blocks[1] == {
            "alt_text": ":frame:",
            "image_url": "https://example.com/" + "a" * 2980,
            "type": "image",
        }
        assert card_to_slack_blocks({"children": [{"type": "image", "url": long_url}], "type": "card"})[0] == {
            "alt_text": "Image",
            "image_url": "https://example.com/" + "a" * 2980,
            "type": "image",
        }

    def test_converts_text_and_links(self) -> None:
        assert card_to_slack_blocks(
            _card(
                [
                    {"content": "plain", "type": "text"},
                    {"content": "bold", "style": "bold", "type": "text"},
                    {"content": "muted", "style": "muted", "type": "text"},
                    {"label": "Docs", "type": "link", "url": "https://example.com"},
                ]
            )
        ) == [
            {"text": {"text": "plain", "type": "mrkdwn"}, "type": "section"},
            {"text": {"text": "*bold*", "type": "mrkdwn"}, "type": "section"},
            {"elements": [{"text": "muted", "type": "mrkdwn"}], "type": "context"},
            {
                "text": {"text": "<https://example.com|Docs>", "type": "mrkdwn"},
                "type": "section",
            },
        ]

    def test_converts_actions(self) -> None:
        blocks = card_to_slack_blocks(
            _card(
                [
                    {
                        "children": [
                            {
                                "id": "approve",
                                "label": "Approve",
                                "style": "primary",
                                "type": "button",
                            },
                            {
                                "label": "Docs",
                                "style": "default",
                                "type": "link-button",
                                "url": "https://example.com/docs",
                            },
                            {
                                "id": "status",
                                "label": "Status",
                                "options": [
                                    {"label": "Open", "value": "open"},
                                    {"label": "Closed", "value": "closed"},
                                ],
                                "placeholder": "Choose",
                                "type": "select",
                            },
                            {
                                "id": "plan",
                                "label": "Plan",
                                "options": [
                                    {"description": "For teams", "label": "Pro", "value": "pro"},
                                ],
                                "type": "radio_select",
                            },
                        ],
                        "type": "actions",
                    },
                ]
            )
        )

        assert blocks[0] == {
            "elements": [
                {
                    "action_id": "approve",
                    "style": "primary",
                    "text": {"emoji": True, "text": "Approve", "type": "plain_text"},
                    "type": "button",
                },
                {
                    "action_id": "link-https://example.com/docs",
                    "text": {"emoji": True, "text": "Docs", "type": "plain_text"},
                    "type": "button",
                    "url": "https://example.com/docs",
                },
                {
                    "action_id": "status",
                    "options": [
                        {"text": {"text": "Open", "type": "plain_text"}, "value": "open"},
                        {"text": {"text": "Closed", "type": "plain_text"}, "value": "closed"},
                    ],
                    "placeholder": {"emoji": True, "text": "Choose", "type": "plain_text"},
                    "type": "static_select",
                },
                {
                    "action_id": "plan",
                    "options": [
                        {
                            "description": {"text": "For teams", "type": "mrkdwn"},
                            "text": {"text": "Pro", "type": "mrkdwn"},
                            "value": "pro",
                        },
                    ],
                    "type": "radio_buttons",
                },
            ],
            "type": "actions",
        }

    def test_link_button_uses_custom_action_id(self) -> None:
        # chat@4.31 (171657a): an explicit ``id`` on a link-button is used as
        # the action_id verbatim (still subject to the action_id length limit).
        blocks = card_to_slack_blocks(
            _card(
                [
                    {
                        "children": [
                            {
                                "id": "agent_slack_auth_signin",
                                "label": "Sign in",
                                "type": "link-button",
                                "url": "https://vercel.com/oauth/authorize",
                            },
                        ],
                        "type": "actions",
                    },
                ]
            )
        )
        assert blocks[0]["elements"][0]["action_id"] == "agent_slack_auth_signin"

    def test_link_button_falls_back_to_url_action_id(self) -> None:
        # No ``id`` → action_id falls back to the ``link-{url}`` form.
        blocks = card_to_slack_blocks(
            _card(
                [
                    {
                        "children": [
                            {
                                "label": "Docs",
                                "type": "link-button",
                                "url": "https://example.com/docs",
                            },
                        ],
                        "type": "actions",
                    },
                ]
            )
        )
        assert blocks[0]["elements"][0]["action_id"] == "link-https://example.com/docs"

    def test_link_button_empty_id_is_used_verbatim(self) -> None:
        # ``??`` semantics (NOT ``or``): an explicit empty-string id is used
        # as-is and does NOT fall back to ``link-{url}``. Locks ``is not None``.
        blocks = card_to_slack_blocks(
            _card(
                [
                    {
                        "children": [
                            {
                                "id": "",
                                "label": "Docs",
                                "type": "link-button",
                                "url": "https://example.com/docs",
                            },
                        ],
                        "type": "actions",
                    },
                ]
            )
        )
        assert blocks[0]["elements"][0]["action_id"] == ""

    def test_link_button_custom_id_truncated_to_action_id_limit(self) -> None:
        # The id path still goes through ``_truncate_text(..., LIMITS.action_id)``.
        long_id = "x" * (LIMITS.action_id + 50)
        blocks = card_to_slack_blocks(
            _card(
                [
                    {
                        "children": [
                            {
                                "id": long_id,
                                "label": "Docs",
                                "type": "link-button",
                                "url": "https://example.com/docs",
                            },
                        ],
                        "type": "actions",
                    },
                ]
            )
        )
        assert blocks[0]["elements"][0]["action_id"] == "x" * LIMITS.action_id

    def test_limits_action_elements_and_select_options(self) -> None:
        blocks = card_to_slack_blocks(
            _card(
                [
                    {
                        "children": [
                            {"id": f"b{index}", "label": f"Button {index}", "type": "button"} for index in range(30)
                        ],
                        "type": "actions",
                    },
                    {
                        "children": [
                            {
                                "id": "select",
                                "label": "Select",
                                "options": [
                                    {"label": f"Option {index}", "value": f"value-{index}"} for index in range(120)
                                ],
                                "type": "select",
                            },
                        ],
                        "type": "actions",
                    },
                ]
            )
        )

        assert len(blocks[0]["elements"]) == 25
        assert len(blocks[1]["elements"][0]["options"]) == 100

    def test_truncates_option_values_to_option_object_limit(self) -> None:
        block = card_to_slack_blocks(
            _card(
                [
                    {
                        "children": [
                            {
                                "id": "select",
                                "label": "Select",
                                "options": [{"label": "Option", "value": "v" * 200}],
                                "type": "select",
                            },
                        ],
                        "type": "actions",
                    },
                ]
            )
        )[0]

        assert block["elements"][0]["options"][0]["value"] == "v" * 150

    def test_matches_truncated_initial_options_for_select_elements(self) -> None:
        long_value = "v" * 200
        block = card_to_slack_blocks(
            _card(
                [
                    {
                        "children": [
                            {
                                "id": "select",
                                "initial_option": long_value,
                                "label": "Select",
                                "options": [{"label": "Option", "value": long_value}],
                                "type": "select",
                            },
                            {
                                "id": "radio",
                                "initial_option": long_value,
                                "label": "Radio",
                                "options": [{"label": "Option", "value": long_value}],
                                "type": "radio_select",
                            },
                        ],
                        "type": "actions",
                    },
                ]
            )
        )[0]

        assert block["elements"][0]["initial_option"]["value"] == "v" * 150
        assert block["elements"][1]["initial_option"]["value"] == "v" * 150

    def test_omits_initial_options_when_no_initial_value(self) -> None:
        block = card_to_slack_blocks(
            _card(
                [
                    {
                        "children": [
                            {
                                "id": "select",
                                "label": "Select",
                                "options": [{"label": "Option", "value": ""}],
                                "type": "select",
                            },
                        ],
                        "type": "actions",
                    },
                ]
            )
        )[0]

        assert "initial_option" not in block["elements"][0]

    def test_converts_fields_and_tables(self) -> None:
        blocks = card_to_slack_blocks(
            _card(
                [
                    {
                        "children": [
                            {"label": "Name", "type": "field", "value": "Ada"},
                            {"label": "Role", "type": "field", "value": "Engineer"},
                        ],
                        "type": "fields",
                    },
                    {
                        "align": ["left", "right"],
                        "headers": ["Name", "Score"],
                        "rows": [["Ada", "10"]],
                        "type": "table",
                    },
                ]
            )
        )

        assert blocks[0] == {
            "fields": [
                {"text": "*Name*\nAda", "type": "mrkdwn"},
                {"text": "*Role*\nEngineer", "type": "mrkdwn"},
            ],
            "type": "section",
        }
        # The data table block has no column_settings; ``align`` is dropped.
        assert blocks[1] == {
            "caption": "Table",
            "rows": [
                [
                    {"text": "Name", "type": "raw_text"},
                    {"text": "Score", "type": "raw_text"},
                ],
                [
                    {"text": "Ada", "type": "raw_text"},
                    {"text": "10", "type": "raw_text"},
                ],
            ],
            "type": "data_table",
        }

    def test_passes_table_caption_and_clamped_page_size_through(self) -> None:
        blocks = card_to_slack_blocks(
            _card([{"caption": "Scores", "headers": ["Name"], "page_size": 0, "rows": [["Ada"]], "type": "table"}])
        )

        assert blocks[0] == {
            "caption": "Scores",
            "page_size": 1,
            "rows": [
                [{"text": "Name", "type": "raw_text"}],
                [{"text": "Ada", "type": "raw_text"}],
            ],
            "type": "data_table",
        }

    def test_renders_header_only_tables_as_a_plain_table_block(self) -> None:
        blocks = card_to_slack_blocks(_card([{"align": ["left"], "headers": ["Name"], "rows": [], "type": "table"}]))

        assert blocks[0] == {
            "column_settings": [{"align": "left"}],
            "rows": [[{"text": "Name", "type": "raw_text"}]],
            "type": "table",
        }

    def test_falls_back_to_ascii_when_combined_table_cells_exceed_the_character_limit(self) -> None:
        big_cell = "x" * 10_001
        blocks = card_to_slack_blocks(_card([{"headers": ["A"], "rows": [[big_cell]], "type": "table"}]))

        assert blocks[0]["type"] == "section"
        text = blocks[0]["text"]["text"]
        assert len(text) == LIMITS.section_text
        # Closing code fence survives truncation, after the ellipsis marker
        assert text.endswith("…\n```")

    def test_converts_pie_charts_to_data_visualization_blocks(self) -> None:
        blocks = card_to_slack_blocks(
            _card(
                [
                    {
                        "chart": {
                            "segments": [{"label": "Kit Kat", "value": 45}, {"label": "Twix", "value": 28}],
                            "type": "pie",
                        },
                        "title": "Candy Bars",
                        "type": "chart",
                    }
                ]
            )
        )

        assert blocks[0] == {
            "chart": {
                "segments": [{"label": "Kit Kat", "value": 45}, {"label": "Twix", "value": 28}],
                "type": "pie",
            },
            "title": "Candy Bars",
            "type": "data_visualization",
        }

    def test_converts_series_charts_with_axis_config_and_normalized_point_order(self) -> None:
        blocks = card_to_slack_blocks(
            _card(
                [
                    {
                        "chart": {
                            "categories": ["Mon", "Tue"],
                            "series": [
                                {
                                    "data": [{"label": "Tue", "value": 60}, {"label": "Mon", "value": 50}],
                                    "name": "Mobile",
                                }
                            ],
                            "type": "line",
                            "x_label": "Day",
                            "y_label": "Users",
                        },
                        "title": "DAU",
                        "type": "chart",
                    }
                ]
            )
        )

        assert blocks[0] == {
            "chart": {
                "axis_config": {"categories": ["Mon", "Tue"], "x_label": "Day", "y_label": "Users"},
                "series": [{"data": [{"label": "Mon", "value": 50}, {"label": "Tue", "value": 60}], "name": "Mobile"}],
                "type": "line",
            },
            "title": "DAU",
            "type": "data_visualization",
        }

    def test_falls_back_to_a_text_section_for_invalid_charts(self) -> None:
        blocks = card_to_slack_blocks(
            _card(
                [
                    {
                        "chart": {"segments": [{"label": "Zero", "value": 0}], "type": "pie"},
                        "title": "Bad Pie",
                        "type": "chart",
                    }
                ]
            )
        )

        assert blocks[0] == {
            "text": {"text": "```\nBad Pie\nLabel | Value\nZero  | 0\n```", "type": "mrkdwn"},
            "type": "section",
        }

    def test_falls_back_to_a_text_section_from_the_third_chart_in_one_message(self) -> None:
        def pie(title: str) -> dict[str, Any]:
            return {"chart": {"segments": [{"label": "A", "value": 1}], "type": "pie"}, "title": title, "type": "chart"}

        blocks = card_to_slack_blocks(_card([pie("One"), pie("Two"), pie("Three")]))

        assert [b["type"] for b in blocks] == ["data_visualization", "data_visualization", "section"]

    def test_includes_chart_data_in_slack_fallback_text(self) -> None:
        text = card_to_slack_fallback_text(
            _card(
                [
                    {
                        "chart": {"segments": [{"label": "Kit Kat", "value": 45}], "type": "pie"},
                        "title": "Candy Bars",
                        "type": "chart",
                    }
                ]
            )
        )

        assert "Candy Bars" in text
        assert "Kit Kat" in text

    def test_series_chart_fallback_text_formats_numbers_like_js_and_looks_points_up_by_category(self) -> None:
        # ``String(value)``: 45.0 renders "45"; a series missing a category gets an empty cell.
        text = card_to_slack_fallback_text(
            _card(
                [
                    {
                        "chart": {
                            "categories": ["Mon", "Tue"],
                            "series": [{"data": [{"label": "Tue", "value": 45.0}], "name": "A"}],
                            "type": "bar",
                            "x_label": "Day",
                        },
                        "title": "Gaps",
                        "type": "chart",
                    }
                ]
            )
        )

        assert text == "Gaps\nDay | A\nMon |\nTue | 45"

    def test_falls_back_to_ascii_tables_after_one_native_table(self) -> None:
        table = {"headers": ["A", "B"], "rows": [["1", "2"]], "type": "table"}

        assert card_to_slack_blocks(_card([table, table]))[1] == {
            "text": {"text": "```\nA | B\n1 | 2\n```", "type": "mrkdwn"},
            "type": "section",
        }

    def test_generates_slack_fallback_text(self) -> None:
        assert (
            card_to_slack_fallback_text(
                {
                    "children": [
                        {"content": "Hello", "type": "text"},
                        {
                            "children": [{"label": "Status", "type": "field", "value": "Ready"}],
                            "type": "fields",
                        },
                        {
                            "children": [{"id": "ok", "label": "OK", "type": "button"}],
                            "type": "actions",
                        },
                    ],
                    "subtitle": "Sub",
                    "title": "Title",
                    "type": "card",
                }
            )
            == "*Title*\nSub\nHello\nStatus: Ready"
        )

    def test_keeps_compatibility_aliases(self) -> None:
        card = _card([{"content": "hello", "type": "text"}])

        assert card_to_block_kit(card) == card_to_slack_blocks(card)
        assert card_to_fallback_text(card) == card_to_slack_fallback_text(card)

    def test_supports_custom_emoji_conversion(self) -> None:
        card = _card([{"content": "{{emoji:thumbs_up}}", "type": "text"}])

        assert card_to_slack_blocks(card)[0] == {
            "text": {"text": ":thumbs_up:", "type": "mrkdwn"},
            "type": "section",
        }
        assert card_to_slack_blocks(card, {"convert_emoji": lambda _text: ":+1:"})[0] == {
            "text": {"text": ":+1:", "type": "mrkdwn"},
            "type": "section",
        }
        assert convert_slack_emoji_placeholders("hi {{emoji:wave}}") == "hi :wave:"

    def test_renders_input_requests_as_slack_buttons(self) -> None:
        assert input_request_to_slack_blocks(
            {
                "options": [
                    {"id": "approve", "label": "Approve", "style": "primary"},
                    {"id": "deny", "label": "Deny", "style": "danger"},
                ],
                "prompt": "Approve deploy?",
                "request_id": "req-1",
            }
        ) == [
            {"text": {"text": "Approve deploy?", "type": "mrkdwn"}, "type": "section"},
            {
                "elements": [
                    {
                        "action_id": "input:req-1:button:0",
                        "style": "primary",
                        "text": {"text": "Approve", "type": "plain_text"},
                        "type": "button",
                        "value": "approve",
                    },
                    {
                        "action_id": "input:req-1:button:1",
                        "style": "danger",
                        "text": {"text": "Deny", "type": "plain_text"},
                        "type": "button",
                        "value": "deny",
                    },
                ],
                "type": "actions",
            },
        ]

    def test_renders_input_requests_as_selects(self) -> None:
        assert input_request_to_slack_blocks(
            {
                "display": "select",
                "options": [{"id": "one", "label": "One"}],
                "prompt": "Pick one",
                "request_id": "req-1",
            }
        )[1] == {
            "elements": [
                {
                    "action_id": "input:req-1",
                    "options": [{"text": {"text": "One", "type": "plain_text"}, "value": "one"}],
                    "placeholder": {"text": "Choose an option", "type": "plain_text"},
                    "type": "static_select",
                },
            ],
            "type": "actions",
        }

    def test_renders_input_requests_as_radios(self) -> None:
        assert input_request_to_slack_blocks(
            {
                "display": "radio",
                "options": [{"id": "one", "label": "One"}],
                "prompt": "Pick one",
                "request_id": "req-1",
            }
        )[1] == {
            "elements": [
                {
                    "action_id": "input:req-1",
                    "options": [{"text": {"text": "One", "type": "plain_text"}, "value": "one"}],
                    "type": "radio_buttons",
                },
            ],
            "type": "actions",
        }

    def test_renders_freeform_alongside_options_when_allowed(self) -> None:
        assert input_request_to_slack_blocks(
            {
                "allow_freeform": True,
                "options": [{"id": "approve", "label": "Approve"}],
                "prompt": "Approve deploy?",
                "request_id": "req-1",
            }
        )[1] == {
            "elements": [
                {
                    "action_id": "input:req-1:button:0",
                    "text": {"text": "Approve", "type": "plain_text"},
                    "type": "button",
                    "value": "approve",
                },
                {
                    "action_id": "input-freeform:req-1",
                    "style": "primary",
                    "text": {"text": "Type your answer", "type": "plain_text"},
                    "type": "button",
                    "value": "req-1",
                },
            ],
            "type": "actions",
        }

    def test_renders_and_reads_freeform_input_modals(self) -> None:
        view = build_slack_freeform_view({"metadata": {"request_id": "req-1"}, "prompt": "Tell me why"})

        assert view["callback_id"] == "input-freeform-submit"
        assert view["private_metadata"] == '{"request_id":"req-1"}'
        assert view["title"] == {"text": "Tell me why", "type": "plain_text"}
        assert view["type"] == "modal"
        assert (
            parse_slack_freeform_value(
                [
                    {
                        "action_id": "input-freeform-text",
                        "block_id": "input-freeform-block",
                        "value": "because",
                    }
                ]
            )
            == "because"
        )

    def test_parses_input_actions_and_answered_blocks(self) -> None:
        assert parse_slack_input_response({"action_id": "input:req-1:button:0", "value": "approve"}) == {
            "option_id": "approve",
            "request_id": "req-1",
        }
        assert parse_slack_input_response({"action_id": "input:req-2", "selected_option_value": "later"}) == {
            "option_id": "later",
            "request_id": "req-2",
        }
        assert answered_slack_input_blocks({"answer": "Approve", "user_id": "U123"}) == [
            {
                "text": {"text": ":white_check_mark: *Approve*", "type": "mrkdwn"},
                "type": "section",
            },
            {
                "elements": [{"text": "Answered by <@U123>", "type": "mrkdwn"}],
                "type": "context",
            },
        ]

    def test_parse_input_response_returns_none_for_unprefixed_actions(self) -> None:
        # Actions without the ``input:`` prefix are not ours to parse.
        assert parse_slack_input_response({"action_id": "other:req-1", "value": "x"}) is None
        # A button action id with no matching ``<id>:button:<n>`` and no
        # selected option value yields no response.
        assert parse_slack_input_response({"action_id": "input:req-1", "value": "x"}) is None

    def test_build_freeform_view_passes_string_metadata_through(self) -> None:
        # A string metadata is used verbatim (not JSON re-encoded).
        view = build_slack_freeform_view({"metadata": "raw-token"})

        assert view["private_metadata"] == "raw-token"
        # With no prompt/title, the title falls back to the default.
        assert view["title"] == {"text": "Your answer", "type": "plain_text"}


class TestBlocksImportBoundary:
    def test_does_not_import_the_full_adapter_or_runtime_packages(self) -> None:
        """Importing the blocks subpath must not pull in slack_sdk, an HTTP
        client, or the high-level adapter module (port of upstream's
        ``blocks/boundary.test.ts``)."""
        code = (
            "import sys\n"
            "import chat_sdk.adapters.slack.blocks\n"
            "forbidden = [\n"
            "    'slack_sdk',\n"
            "    'httpx',\n"
            "    'aiohttp',\n"
            "    'chat_sdk.adapters.slack.adapter',\n"
            "]\n"
            "loaded = [name for name in forbidden if name in sys.modules]\n"
            "assert not loaded, f'blocks subpath imported runtime modules: {loaded}'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
