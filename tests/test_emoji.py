"""Tests for chat_sdk.emoji module."""

from __future__ import annotations

from chat_sdk.emoji import (
    convert_emoji_placeholders,
    emoji_to_gchat,
    emoji_to_slack,
    get_emoji,
    resolve_emoji_from_gchat,
    resolve_emoji_from_slack,
)
from chat_sdk.types import EmojiValue


class TestGetEmoji:
    """Tests for get_emoji singleton."""

    def test_returns_emoji_value(self):
        emoji = get_emoji("thumbs_up")
        assert isinstance(emoji, EmojiValue)
        assert emoji.name == "thumbs_up"

    def test_singleton_identity(self):
        a = get_emoji("wave")
        b = get_emoji("wave")
        assert a is b

    def test_different_names_are_different(self):
        a = get_emoji("heart")
        b = get_emoji("star")
        assert a is not b
        assert a != b

    def test_custom_name(self):
        emoji = get_emoji("my_custom_emoji")
        assert emoji.name == "my_custom_emoji"
        # Calling again returns the same instance
        assert get_emoji("my_custom_emoji") is emoji


class TestEmojiToSlack:
    """Tests for emoji_to_slack."""

    def test_known_emoji(self):
        assert emoji_to_slack("thumbs_up") == "+1"

    def test_known_emoji_from_value(self):
        emoji = EmojiValue(name="thumbs_up")
        assert emoji_to_slack(emoji) == "+1"

    def test_heart_emoji(self):
        assert emoji_to_slack("heart") == "heart"

    def test_rocket_emoji(self):
        assert emoji_to_slack("rocket") == "rocket"

    def test_unknown_emoji_passes_through(self):
        assert emoji_to_slack("nonexistent_emoji") == "nonexistent_emoji"

    def test_list_value_returns_first(self):
        # thumbs_up maps to ["+1", "thumbsup"], should return first
        result = emoji_to_slack("thumbs_up")
        assert result == "+1"

    def test_smile_returns_first_alias(self):
        # smile maps to ["smile", "slightly_smiling_face"]
        result = emoji_to_slack("smile")
        assert result == "smile"


class TestEmojiToGchat:
    """Tests for emoji_to_gchat."""

    def test_known_emoji(self):
        result = emoji_to_gchat("thumbs_up")
        assert result == "\U0001f44d"  # thumbs up unicode

    def test_known_emoji_from_value(self):
        emoji = EmojiValue(name="rocket")
        result = emoji_to_gchat(emoji)
        assert result == "\U0001f680"  # rocket unicode

    def test_unknown_emoji_passes_through(self):
        assert emoji_to_gchat("custom_thing") == "custom_thing"

    def test_heart(self):
        result = emoji_to_gchat("heart")
        assert result == "\u2764\ufe0f"


class TestConvertEmojiPlaceholders:
    """Tests for convert_emoji_placeholders."""

    def test_slack_platform(self):
        text = "Hello {{emoji:wave}} there"
        result = convert_emoji_placeholders(text, "slack")
        assert ":wave:" in result
        assert "{{emoji:" not in result

    def test_gchat_platform(self):
        text = "Check {{emoji:check}}"
        result = convert_emoji_placeholders(text, "gchat")
        assert "{{emoji:" not in result
        # Should contain a unicode character
        assert len(result) > len("Check ")

    def test_default_platform_uses_unicode(self):
        text = "{{emoji:fire}}"
        result = convert_emoji_placeholders(text, "whatsapp")
        assert "{{emoji:" not in result

    def test_no_placeholders(self):
        text = "Just plain text"
        assert convert_emoji_placeholders(text, "slack") == "Just plain text"

    def test_multiple_placeholders(self):
        text = "{{emoji:thumbs_up}} and {{emoji:heart}}"
        result = convert_emoji_placeholders(text, "slack")
        assert ":+1:" in result
        assert ":heart:" in result

    def test_unknown_emoji_placeholder(self):
        text = "{{emoji:unknown_custom}}"
        result = convert_emoji_placeholders(text, "slack")
        assert ":unknown_custom:" in result


class TestResolveEmojiFromSlack:
    """Tests for resolve_emoji_from_slack."""

    def test_known_slack_name(self):
        emoji = resolve_emoji_from_slack("+1")
        assert emoji.name == "thumbs_up"

    def test_known_slack_alias(self):
        emoji = resolve_emoji_from_slack("thumbsup")
        assert emoji.name == "thumbs_up"

    def test_unknown_slack_name(self):
        emoji = resolve_emoji_from_slack("custom_emoji")
        assert emoji.name == "custom_emoji"

    def test_returns_emoji_value(self):
        emoji = resolve_emoji_from_slack("heart")
        assert isinstance(emoji, EmojiValue)

    def test_rocket(self):
        emoji = resolve_emoji_from_slack("rocket")
        assert emoji.name == "rocket"

    def test_tada_maps_to_party(self):
        emoji = resolve_emoji_from_slack("tada")
        assert emoji.name == "party"


class TestResolveEmojiFromGchat:
    """Tests for resolve_emoji_from_gchat."""

    def test_known_gchat_unicode(self):
        emoji = resolve_emoji_from_gchat("\U0001f44d")  # thumbs up
        assert emoji.name == "thumbs_up"

    def test_unknown_gchat_emoji(self):
        emoji = resolve_emoji_from_gchat("some_unknown")
        assert emoji.name == "some_unknown"

    def test_returns_emoji_value(self):
        emoji = resolve_emoji_from_gchat("\U0001f680")  # rocket
        assert isinstance(emoji, EmojiValue)
