"""Tests for the Telegram adapter."""

from __future__ import annotations

import os

import pytest

from chat_sdk.adapters.telegram.adapter import (
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_MESSAGE_LIMIT,
    TelegramAdapter,
    apply_telegram_entities,
    create_telegram_adapter,
)
from chat_sdk.adapters.telegram.types import TelegramAdapterConfig, TelegramThreadId
from chat_sdk.shared.errors import ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(**overrides) -> TelegramAdapter:
    """Create a TelegramAdapter with minimal valid config."""
    config = TelegramAdapterConfig(
        bot_token=overrides.pop("bot_token", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"),
        **overrides,
    )
    return TelegramAdapter(config)


# ---------------------------------------------------------------------------
# Thread ID encode / decode
# ---------------------------------------------------------------------------


class TestTelegramThreadId:
    """Thread ID encoding and decoding."""

    def test_encode_without_topic(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(TelegramThreadId(chat_id="-1001234567890"))
        assert tid == "telegram:-1001234567890"

    def test_encode_with_topic(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(TelegramThreadId(chat_id="-1001234567890", message_thread_id=42))
        assert tid == "telegram:-1001234567890:42"

    def test_decode_without_topic(self):
        adapter = _make_adapter()
        decoded = adapter.decode_thread_id("telegram:-1001234567890")
        assert decoded.chat_id == "-1001234567890"
        assert decoded.message_thread_id is None

    def test_decode_with_topic(self):
        adapter = _make_adapter()
        decoded = adapter.decode_thread_id("telegram:-1001234567890:42")
        assert decoded.chat_id == "-1001234567890"
        assert decoded.message_thread_id == 42

    def test_roundtrip_without_topic(self):
        adapter = _make_adapter()
        original = TelegramThreadId(chat_id="12345")
        encoded = adapter.encode_thread_id(original)
        decoded = adapter.decode_thread_id(encoded)
        assert decoded.chat_id == original.chat_id
        assert decoded.message_thread_id is None

    def test_roundtrip_with_topic(self):
        adapter = _make_adapter()
        original = TelegramThreadId(chat_id="-100999", message_thread_id=7)
        encoded = adapter.encode_thread_id(original)
        decoded = adapter.decode_thread_id(encoded)
        assert decoded.chat_id == original.chat_id
        assert decoded.message_thread_id == original.message_thread_id

    def test_decode_invalid_prefix(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("slack:C123:ts")

    def test_decode_empty_chat_id(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("telegram:")

    def test_decode_too_many_parts(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("telegram:a:b:c:d")

    def test_decode_non_numeric_topic(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("telegram:12345:not_a_number")

    def test_positive_chat_id_is_dm(self):
        adapter = _make_adapter()
        assert adapter.is_dm("telegram:12345") is True

    def test_negative_chat_id_is_not_dm(self):
        adapter = _make_adapter()
        assert adapter.is_dm("telegram:-100123") is False


# ---------------------------------------------------------------------------
# apply_telegram_entities
# ---------------------------------------------------------------------------


class TestApplyTelegramEntities:
    """Tests for apply_telegram_entities."""

    def test_no_entities(self):
        assert apply_telegram_entities("Hello world", []) == "Hello world"

    def test_bold_entity(self):
        result = apply_telegram_entities(
            "Hello world",
            [{"type": "bold", "offset": 0, "length": 5}],
        )
        assert result == "**Hello** world"

    def test_italic_entity(self):
        result = apply_telegram_entities(
            "Hello world",
            [{"type": "italic", "offset": 6, "length": 5}],
        )
        assert result == "Hello *world*"

    def test_code_entity(self):
        result = apply_telegram_entities(
            "Use print here",
            [{"type": "code", "offset": 4, "length": 5}],
        )
        assert result == "Use `print` here"

    def test_pre_entity(self):
        result = apply_telegram_entities(
            "Code block here",
            [{"type": "pre", "offset": 0, "length": 10}],
        )
        assert "```" in result
        assert "Code block" in result

    def test_pre_entity_with_language(self):
        result = apply_telegram_entities(
            "print(1)",
            [{"type": "pre", "offset": 0, "length": 8, "language": "python"}],
        )
        assert "```python" in result

    def test_strikethrough_entity(self):
        result = apply_telegram_entities(
            "Hello world",
            [{"type": "strikethrough", "offset": 0, "length": 5}],
        )
        assert result == "~~Hello~~ world"

    def test_text_link_entity(self):
        result = apply_telegram_entities(
            "Click here for more",
            [{"type": "text_link", "offset": 6, "length": 4, "url": "https://example.com"}],
        )
        assert "[here](https://example.com)" in result

    def test_multiple_entities(self):
        result = apply_telegram_entities(
            "Hello world",
            [
                {"type": "bold", "offset": 0, "length": 5},
                {"type": "italic", "offset": 6, "length": 5},
            ],
        )
        assert "**Hello**" in result
        assert "*world*" in result

    def test_empty_text(self):
        assert apply_telegram_entities("", []) == ""

    def test_entity_with_unicode(self):
        # Emoji takes 2 UTF-16 code units, so offsets shift:
        # H=0, i=1, ' '=2, emoji=3-4, ' '=5, t=6, h=7, e=8, r=9, e=10
        text = "Hi \U0001f600 there"
        result = apply_telegram_entities(
            text,
            [{"type": "bold", "offset": 6, "length": 5}],
        )
        assert "**there**" in result


# ---------------------------------------------------------------------------
# encode_message_id / decode_composite_message_id
# ---------------------------------------------------------------------------


class TestMessageIdEncoding:
    """Tests for encode_message_id and decode_composite_message_id."""

    def test_encode(self):
        adapter = _make_adapter()
        result = adapter.encode_message_id("12345", 99)
        assert result == "12345:99"

    def test_decode_composite(self):
        adapter = _make_adapter()
        result = adapter.decode_composite_message_id("12345:99")
        assert result["chat_id"] == "12345"
        assert result["message_id"] == 99
        assert result["composite_id"] == "12345:99"

    def test_decode_with_expected_chat_id(self):
        adapter = _make_adapter()
        result = adapter.decode_composite_message_id("12345:99", expected_chat_id="12345")
        assert result["chat_id"] == "12345"

    def test_decode_mismatch_chat_id(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError, match="mismatch"):
            adapter.decode_composite_message_id("12345:99", expected_chat_id="67890")

    def test_decode_plain_integer_with_expected(self):
        adapter = _make_adapter()
        result = adapter.decode_composite_message_id("99", expected_chat_id="12345")
        assert result["chat_id"] == "12345"
        assert result["message_id"] == 99

    def test_decode_plain_without_expected_raises(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError, match="format"):
            adapter.decode_composite_message_id("99")

    def test_roundtrip(self):
        adapter = _make_adapter()
        encoded = adapter.encode_message_id("-100999", 42)
        decoded = adapter.decode_composite_message_id(encoded)
        assert decoded["chat_id"] == "-100999"
        assert decoded["message_id"] == 42


# ---------------------------------------------------------------------------
# truncate_message / truncate_caption
# ---------------------------------------------------------------------------


class TestTruncation:
    """Tests for truncate_message and truncate_caption."""

    def test_message_within_limit(self):
        adapter = _make_adapter()
        text = "short message"
        assert adapter.truncate_message(text) == text

    def test_message_at_limit(self):
        adapter = _make_adapter()
        text = "x" * TELEGRAM_MESSAGE_LIMIT
        assert adapter.truncate_message(text) == text

    def test_message_over_limit(self):
        adapter = _make_adapter()
        text = "x" * (TELEGRAM_MESSAGE_LIMIT + 100)
        result = adapter.truncate_message(text)
        assert len(result) == TELEGRAM_MESSAGE_LIMIT
        assert result.endswith("...")

    def test_caption_within_limit(self):
        adapter = _make_adapter()
        text = "short caption"
        assert adapter.truncate_caption(text) == text

    def test_caption_at_limit(self):
        adapter = _make_adapter()
        text = "x" * TELEGRAM_CAPTION_LIMIT
        assert adapter.truncate_caption(text) == text

    def test_caption_over_limit(self):
        adapter = _make_adapter()
        text = "x" * (TELEGRAM_CAPTION_LIMIT + 100)
        result = adapter.truncate_caption(text)
        assert len(result) == TELEGRAM_CAPTION_LIMIT
        assert result.endswith("...")


# ---------------------------------------------------------------------------
# normalize_user_name
# ---------------------------------------------------------------------------


class TestNormalizeUserName:
    """Tests for normalize_user_name."""

    def test_plain_name(self):
        adapter = _make_adapter()
        assert adapter.normalize_user_name("mybot") == "mybot"

    def test_leading_at(self):
        adapter = _make_adapter()
        assert adapter.normalize_user_name("@mybot") == "mybot"

    def test_multiple_leading_at(self):
        adapter = _make_adapter()
        assert adapter.normalize_user_name("@@mybot") == "mybot"

    def test_empty_string(self):
        adapter = _make_adapter()
        assert adapter.normalize_user_name("") == "bot"

    def test_just_at(self):
        adapter = _make_adapter()
        assert adapter.normalize_user_name("@") == "bot"

    def test_non_string(self):
        adapter = _make_adapter()
        assert adapter.normalize_user_name(None) == "bot"
        assert adapter.normalize_user_name(42) == "bot"


# ---------------------------------------------------------------------------
# create_telegram_adapter factory
# ---------------------------------------------------------------------------


class TestCreateTelegramAdapter:
    """Tests for create_telegram_adapter factory."""

    def test_with_bot_token_in_config(self):
        config = TelegramAdapterConfig(bot_token="123:ABC")
        adapter = create_telegram_adapter(config)
        assert adapter.name == "telegram"

    def test_missing_bot_token(self):
        old = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        try:
            with pytest.raises(ValidationError, match="botToken"):
                create_telegram_adapter(TelegramAdapterConfig())
        finally:
            if old is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = old

    def test_from_env_var(self):
        old = os.environ.get("TELEGRAM_BOT_TOKEN")
        os.environ["TELEGRAM_BOT_TOKEN"] = "env-token-123:XYZ"
        try:
            adapter = create_telegram_adapter(TelegramAdapterConfig())
            assert adapter.name == "telegram"
        finally:
            if old is None:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            else:
                os.environ["TELEGRAM_BOT_TOKEN"] = old

    def test_invalid_mode(self):
        with pytest.raises(ValidationError, match="Invalid mode"):
            TelegramAdapter(
                TelegramAdapterConfig(
                    bot_token="123:ABC",
                    mode="invalid_mode",
                )
            )

    def test_adapter_class_properties(self):
        adapter = _make_adapter()
        assert adapter.name == "telegram"
        assert adapter.lock_scope == "channel"
        assert adapter.persist_message_history is True
        assert adapter.bot_user_id is None  # not yet initialized
        assert adapter.is_polling is False
        assert adapter.runtime_mode == "webhook"
