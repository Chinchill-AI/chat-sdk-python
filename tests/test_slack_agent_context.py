"""Port of upstream ``packages/adapter-slack/src/agent-context.test.ts`` (vercel/chat 1721fa01)."""

from __future__ import annotations

from typing import Any

import pytest

from chat_sdk.adapters.slack.agent_context import get_app_context, normalize_app_context_entities
from chat_sdk.testing import create_test_message
from chat_sdk.types import (
    AppContextCanvasEntity,
    AppContextChannelEntity,
    AppContextListEntity,
    AppContextMessageEntity,
    AppContextUnknownEntity,
)


class TestNormalizeAppContextEntities:
    # TS: "returns [] for an empty context object"
    def test_returns_empty_list_for_an_empty_context_object(self):
        assert normalize_app_context_entities({}) == []

    # TS: "returns [] for a missing context"
    def test_returns_empty_list_for_a_missing_context(self):
        assert normalize_app_context_entities(None) == []

    # TS: "maps channel_id"
    def test_maps_channel_id(self):
        assert normalize_app_context_entities({"entities": [{"type": "slack#/types/channel_id", "value": "C123"}]}) == [
            AppContextChannelEntity(channel_id="C123")
        ]

    # TS: "maps canvas_id and list_id"
    def test_maps_canvas_id_and_list_id(self):
        assert normalize_app_context_entities(
            {
                "entities": [
                    {"type": "slack#/types/canvas_id", "value": "F1"},
                    {"type": "slack#/types/list_id", "value": "L1"},
                ]
            }
        ) == [AppContextCanvasEntity(canvas_id="F1"), AppContextListEntity(list_id="L1")]

    # TS: "maps message_context"
    def test_maps_message_context(self):
        assert normalize_app_context_entities(
            {
                "entities": [
                    {
                        "type": "slack#/types/message_context",
                        "value": {"message_ts": "111.222", "channel_id": "C9"},
                    }
                ]
            }
        ) == [AppContextMessageEntity(message_ts="111.222", channel_id="C9")]

    # TS: "maps unrecognized tokens to kind unknown"
    def test_maps_unrecognized_tokens_to_kind_unknown(self):
        assert normalize_app_context_entities({"entities": [{"type": "slack#/types/future", "value": 42}]}) == [
            AppContextUnknownEntity(type="slack#/types/future", value=42)
        ]

    # TS: "maps a message_context with a malformed value to kind unknown instead of throwing"
    def test_maps_a_message_context_with_a_malformed_value_to_kind_unknown_instead_of_throwing(self):
        assert normalize_app_context_entities(
            {
                "entities": [
                    {"type": "slack#/types/message_context", "value": None},
                    {"type": "slack#/types/message_context", "value": "not-an-object"},
                    {"type": "slack#/types/message_context", "value": {"message_ts": 1}},
                ]
            }
        ) == [
            AppContextUnknownEntity(type="slack#/types/message_context", value=None),
            AppContextUnknownEntity(type="slack#/types/message_context", value="not-an-object"),
            AppContextUnknownEntity(type="slack#/types/message_context", value={"message_ts": 1}),
        ]

    # TS: "preserves team_id/enterprise_id and relevance order"
    def test_preserves_team_id_enterprise_id_and_relevance_order(self):
        assert normalize_app_context_entities(
            {
                "entities": [
                    {"type": "slack#/types/channel_id", "value": "C1", "team_id": "T1"},
                    {"type": "slack#/types/canvas_id", "value": "F1", "enterprise_id": "E1"},
                ]
            }
        ) == [
            AppContextChannelEntity(channel_id="C1", team_id="T1"),
            AppContextCanvasEntity(canvas_id="F1", enterprise_id="E1"),
        ]

    # Python-specific: upstream throws (a webhook 500 and a Slack retry loop)
    # on a non-array ``entities`` or a null entity; here malformed shapes
    # degrade to [] / kind "unknown" and never raise.
    @pytest.mark.parametrize(
        ("context", "expected"),
        [
            ("not-an-object", []),
            ({"entities": "nope"}, []),
            ({"entities": {"type": "slack#/types/channel_id"}}, []),
            ({"entities": [None]}, [AppContextUnknownEntity(type="", value=None)]),
            ({"entities": ["raw"]}, [AppContextUnknownEntity(type="", value="raw")]),
            ({"entities": []}, []),
        ],
    )
    def test_malformed_context_shapes_never_raise(self, context: Any, expected: list[Any]):
        assert normalize_app_context_entities(context) == expected


class TestGetAppContext:
    # TS: "reads and normalizes folded app_context from message.raw"
    def test_reads_and_normalizes_folded_app_context_from_message_raw(self):
        message = create_test_message(
            "m1",
            "hi",
            raw={"app_context": {"entities": [{"type": "slack#/types/channel_id", "value": "C1"}]}},
        )
        assert get_app_context(message) == [AppContextChannelEntity(channel_id="C1")]

    # TS: "returns [] when the message has no folded app_context"
    def test_returns_empty_list_when_the_message_has_no_folded_app_context(self):
        assert get_app_context(create_test_message("m1", "hi", raw={})) == []
