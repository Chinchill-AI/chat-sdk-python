"""Tests for the GitHub adapter."""

from __future__ import annotations

import hashlib
import hmac
import os

import pytest

from chat_sdk.adapters.github.adapter import (
    EMOJI_TO_GITHUB_REACTION,
    GitHubAdapter,
    create_github_adapter,
)
from chat_sdk.adapters.github.types import GitHubThreadId
from chat_sdk.logger import ConsoleLogger
from chat_sdk.shared.errors import ValidationError
from chat_sdk.types import EmojiValue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(**overrides) -> GitHubAdapter:
    """Create a GitHubAdapter with minimal valid config."""
    defaults = {
        "webhook_secret": "test-webhook-secret",
        "token": "ghp_testtoken",
        "logger": ConsoleLogger("error"),
    }
    defaults.update(overrides)
    return GitHubAdapter(defaults)


# ---------------------------------------------------------------------------
# Thread ID encode / decode
# ---------------------------------------------------------------------------


class TestGitHubThreadId:
    """Thread ID encoding and decoding."""

    def test_encode_pr_level(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(GitHubThreadId(owner="octocat", repo="hello-world", pr_number=42))
        assert tid == "github:octocat/hello-world:42"

    def test_decode_pr_level(self):
        adapter = _make_adapter()
        decoded = adapter.decode_thread_id("github:octocat/hello-world:42")
        assert decoded.owner == "octocat"
        assert decoded.repo == "hello-world"
        assert decoded.pr_number == 42
        assert decoded.review_comment_id is None

    def test_encode_review_comment(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(
            GitHubThreadId(
                owner="octocat",
                repo="hello-world",
                pr_number=42,
                review_comment_id=999,
            )
        )
        assert tid == "github:octocat/hello-world:42:rc:999"

    def test_decode_review_comment(self):
        adapter = _make_adapter()
        decoded = adapter.decode_thread_id("github:octocat/hello-world:42:rc:999")
        assert decoded.owner == "octocat"
        assert decoded.repo == "hello-world"
        assert decoded.pr_number == 42
        assert decoded.review_comment_id == 999

    def test_roundtrip_pr_level(self):
        adapter = _make_adapter()
        original = GitHubThreadId(owner="org", repo="project", pr_number=7)
        encoded = adapter.encode_thread_id(original)
        decoded = adapter.decode_thread_id(encoded)
        assert decoded.owner == original.owner
        assert decoded.repo == original.repo
        assert decoded.pr_number == original.pr_number
        assert decoded.review_comment_id is None

    def test_roundtrip_review_comment(self):
        adapter = _make_adapter()
        original = GitHubThreadId(owner="org", repo="project", pr_number=15, review_comment_id=123)
        encoded = adapter.encode_thread_id(original)
        decoded = adapter.decode_thread_id(encoded)
        assert decoded.owner == original.owner
        assert decoded.repo == original.repo
        assert decoded.pr_number == original.pr_number
        assert decoded.review_comment_id == original.review_comment_id

    def test_decode_invalid_prefix(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("slack:C123:1234567890.123456")

    def test_decode_malformed(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("github:malformed")

    def test_decode_empty_after_prefix(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("github:")


# ---------------------------------------------------------------------------
# channel_id_from_thread_id
# ---------------------------------------------------------------------------


class TestChannelIdFromThreadId:
    """Tests for channel_id_from_thread_id."""

    def test_pr_level_thread(self):
        adapter = _make_adapter()
        channel = adapter.channel_id_from_thread_id("github:octocat/hello-world:42")
        assert channel == "github:octocat/hello-world"

    def test_review_comment_thread(self):
        adapter = _make_adapter()
        channel = adapter.channel_id_from_thread_id("github:octocat/hello-world:42:rc:999")
        assert channel == "github:octocat/hello-world"


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------


class TestGitHubVerifySignature:
    """Tests for _verify_signature."""

    def test_valid_signature(self):
        adapter = _make_adapter(webhook_secret="my-secret")
        body = '{"action": "created"}'
        sig = (
            "sha256="
            + hmac.new(
                b"my-secret",
                body.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )
        assert adapter._verify_signature(body, sig) is True

    def test_invalid_signature(self):
        adapter = _make_adapter(webhook_secret="my-secret")
        assert adapter._verify_signature("body", "sha256=wrong") is False

    def test_none_signature(self):
        adapter = _make_adapter()
        assert adapter._verify_signature("body", None) is False

    def test_empty_signature(self):
        adapter = _make_adapter()
        assert adapter._verify_signature("body", "") is False


# ---------------------------------------------------------------------------
# emoji_to_github_reaction
# ---------------------------------------------------------------------------


class TestEmojiToGitHubReaction:
    """Tests for _emoji_to_github_reaction and EMOJI_TO_GITHUB_REACTION map."""

    def test_thumbs_up_string(self):
        adapter = _make_adapter()
        assert adapter._emoji_to_github_reaction("thumbs_up") == "+1"

    def test_thumbs_up_value(self):
        adapter = _make_adapter()
        emoji = EmojiValue(name="thumbs_up")
        assert adapter._emoji_to_github_reaction(emoji) == "+1"

    def test_thumbs_down(self):
        adapter = _make_adapter()
        assert adapter._emoji_to_github_reaction("thumbs_down") == "-1"

    def test_heart(self):
        adapter = _make_adapter()
        assert adapter._emoji_to_github_reaction("heart") == "heart"

    def test_rocket(self):
        adapter = _make_adapter()
        assert adapter._emoji_to_github_reaction("rocket") == "rocket"

    def test_party_maps_to_hooray(self):
        adapter = _make_adapter()
        assert adapter._emoji_to_github_reaction("party") == "hooray"

    def test_confetti_maps_to_hooray(self):
        adapter = _make_adapter()
        assert adapter._emoji_to_github_reaction("confetti") == "hooray"

    def test_unknown_defaults_to_plus_one(self):
        adapter = _make_adapter()
        assert adapter._emoji_to_github_reaction("unknown_emoji") == "+1"

    def test_smile_maps_to_laugh(self):
        adapter = _make_adapter()
        assert adapter._emoji_to_github_reaction("smile") == "laugh"

    def test_map_values_are_valid_github_reactions(self):
        valid_reactions = {"+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"}
        for value in EMOJI_TO_GITHUB_REACTION.values():
            assert value in valid_reactions, f"Invalid GitHub reaction: {value}"


# ---------------------------------------------------------------------------
# create_github_adapter factory
# ---------------------------------------------------------------------------


class TestCreateGitHubAdapter:
    """Tests for create_github_adapter factory."""

    def test_with_token_config(self):
        adapter = create_github_adapter(
            {
                "webhook_secret": "secret",
                "token": "ghp_abc",
            }
        )
        assert adapter.name == "github"

    def test_missing_webhook_secret(self):
        # Clear env var
        old = os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
        try:
            with pytest.raises(ValidationError, match="webhookSecret"):
                create_github_adapter({})
        finally:
            if old is not None:
                os.environ["GITHUB_WEBHOOK_SECRET"] = old

    def test_missing_auth(self):
        old_token = os.environ.pop("GITHUB_TOKEN", None)
        old_app = os.environ.pop("GITHUB_APP_ID", None)
        old_key = os.environ.pop("GITHUB_PRIVATE_KEY", None)
        try:
            with pytest.raises(ValidationError, match="Authentication"):
                create_github_adapter({"webhook_secret": "sec"})
        finally:
            if old_token is not None:
                os.environ["GITHUB_TOKEN"] = old_token
            if old_app is not None:
                os.environ["GITHUB_APP_ID"] = old_app
            if old_key is not None:
                os.environ["GITHUB_PRIVATE_KEY"] = old_key

    def test_adapter_properties(self):
        adapter = _make_adapter()
        assert adapter.name == "github"
        assert adapter.lock_scope is None
        assert adapter.persist_message_history is None
        assert adapter.bot_user_id is None
