"""Port of adapter-twilio/src/format/index.test.ts + src/markdown.test.ts.

Covers the SMS length helpers (``truncate_twilio_text`` /
``twilio_text_or_placeholder``), the ``TwilioFormatConverter`` (plain
pass-through, markdown preserved literally, tables rewritten to ASCII
blocks), and the Python-specific config env-fallback semantics for the
scaffolding added in PR 1.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from chat_sdk.adapters.twilio.format_converter import (
    TWILIO_MESSAGE_LIMIT,
    TwilioFormatConverter,
    TwilioTextResult,
    truncate_twilio_text,
    twilio_text_or_placeholder,
)
from chat_sdk.adapters.twilio.types import (
    ENV_MESSAGING_SERVICE_SID,
    ENV_PHONE_NUMBER,
    TwilioAdapterConfig,
)

converter = TwilioFormatConverter()


# ---------------------------------------------------------------------------
# Length helpers (format/index.test.ts)
# ---------------------------------------------------------------------------


class TestTwilioFormatHelpers:
    """Tests for the Twilio SMS length helpers."""

    def test_keeps_text_within_the_twilio_message_limit(self):
        text = "x" * TWILIO_MESSAGE_LIMIT
        assert truncate_twilio_text(text) == TwilioTextResult(text=text, truncated=False)

    def test_truncates_text_over_the_twilio_message_limit(self):
        result = truncate_twilio_text("x" * (TWILIO_MESSAGE_LIMIT + 1))
        assert len(result.text) == TWILIO_MESSAGE_LIMIT
        assert result.truncated is True

    @pytest.mark.parametrize("limit", [0, -1, 1.5, True])
    def test_rejects_invalid_limits(self, limit):
        # ``True`` is an int subclass in Python; upstream's Number.isInteger
        # rejects booleans, so the port must too (input sweep, not just 0).
        with pytest.raises(TypeError, match="limit must be a positive integer"):
            truncate_twilio_text("hello", limit=limit)

    def test_truncates_at_a_custom_limit(self):
        result = truncate_twilio_text("hello", limit=2)
        assert result == TwilioTextResult(text="he", truncated=True)

    def test_uses_a_placeholder_for_empty_bodies(self):
        assert twilio_text_or_placeholder("") == " "
        assert twilio_text_or_placeholder("hello") == "hello"


# ---------------------------------------------------------------------------
# Format converter (markdown.test.ts)
# ---------------------------------------------------------------------------


class TestTwilioFormatConverter:
    """Tests for TwilioFormatConverter."""

    def test_keeps_raw_strings_plain(self):
        assert converter.render_postable("hello") == "hello"

    def test_converts_markdown_to_twilio_text(self):
        # SMS renders no markdown: markers survive as literal text.
        assert converter.render_postable({"markdown": "**hello**"}) == "**hello**"

    def test_renders_tables_as_ascii_blocks(self):
        text = converter.from_ast(converter.to_ast("| name | age |\n| --- | --- |\n| Ada | 36 |"))
        assert "name | age" in text
        assert "| --- |" not in text

    def test_renders_raw_postable_shape(self):
        assert converter.render_postable({"raw": "raw *text*"}) == "raw *text*"

    def test_renders_ast_postable_shape(self):
        ast = converter.to_ast("hello from ast")
        assert converter.render_postable({"ast": ast}) == "hello from ast"

    def test_to_ast_parses_markdown_into_root(self):
        ast = converter.to_ast("**bold** text")
        assert ast["type"] == "root"
        assert len(ast.get("children", [])) > 0


# ---------------------------------------------------------------------------
# Config env fallbacks (Python scaffolding)
# ---------------------------------------------------------------------------


@pytest.fixture
def _clear_twilio_sender_env() -> Iterator[None]:
    """Save and clear the TWILIO_* sender env vars for a test."""
    saved: dict[str, str | None] = {k: os.environ.pop(k, None) for k in (ENV_MESSAGING_SERVICE_SID, ENV_PHONE_NUMBER)}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class TestTwilioAdapterConfig:
    """Env-fallback semantics for the sender fields (`??`, not `||`)."""

    def test_all_fields_are_optional(self, _clear_twilio_sender_env: None):
        config = TwilioAdapterConfig()
        assert config.resolved_messaging_service_sid() is None
        assert config.resolved_phone_number() is None

    def test_explicit_values_win_over_env(self, _clear_twilio_sender_env: None):
        os.environ[ENV_MESSAGING_SERVICE_SID] = "MGenv"
        os.environ[ENV_PHONE_NUMBER] = "+15550000009"
        config = TwilioAdapterConfig(messaging_service_sid="MG123", phone_number="+15550000001")
        assert config.resolved_messaging_service_sid() == "MG123"
        assert config.resolved_phone_number() == "+15550000001"

    def test_env_fallback_when_omitted(self, _clear_twilio_sender_env: None):
        os.environ[ENV_MESSAGING_SERVICE_SID] = "MGenv"
        os.environ[ENV_PHONE_NUMBER] = "+15550000009"
        config = TwilioAdapterConfig()
        assert config.resolved_messaging_service_sid() == "MGenv"
        assert config.resolved_phone_number() == "+15550000009"

    def test_empty_string_does_not_fall_back_to_env(self, _clear_twilio_sender_env: None):
        # Upstream uses `??` (nullish), so an explicit empty string must NOT
        # be replaced by the env var — the `or` truthiness trap would.
        os.environ[ENV_MESSAGING_SERVICE_SID] = "MGenv"
        os.environ[ENV_PHONE_NUMBER] = "+15550000009"
        config = TwilioAdapterConfig(messaging_service_sid="", phone_number="")
        assert config.resolved_messaging_service_sid() == ""
        assert config.resolved_phone_number() == ""
