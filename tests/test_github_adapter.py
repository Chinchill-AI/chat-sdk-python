"""Tests for the GitHub adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.github.adapter import (
    EMOJI_TO_GITHUB_REACTION,
    GitHubAdapter,
    create_github_adapter,
)
from chat_sdk.adapters.github.types import GitHubThreadId
from chat_sdk.logger import ConsoleLogger
from chat_sdk.shared.errors import ValidationError
from chat_sdk.testing import MockLogger
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


# ---------------------------------------------------------------------------
# Bot user id: GITHUB_BOT_USER_ID env var and learned-from-post fallback
# (upstream 6750d59e, chat@4.33; #233)
# ---------------------------------------------------------------------------


def _posted_comment(user: object) -> dict:
    return {
        "id": 100,
        "body": "hi",
        "user": user,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "html_url": "https://github.com/acme/app/issues/42#issuecomment-100",
    }


class _WebhookRequest:
    def __init__(self, body: str, headers: dict[str, str]) -> None:
        self.body = body.encode("utf-8")
        self.headers = headers


def _signed_issue_comment_request(sender_id: int) -> _WebhookRequest:
    payload = {
        "action": "created",
        "comment": {
            "id": 101,
            "body": "echo of a reply",
            "user": {"id": sender_id, "login": "someone", "type": "User"},
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        },
        "issue": {"number": 42, "title": "Issue"},
        "repository": {
            "id": 1,
            "name": "app",
            "full_name": "acme/app",
            "owner": {"id": 10, "login": "acme", "type": "Organization"},
        },
        "sender": {"id": sender_id, "login": "someone", "type": "User"},
    }
    body = json.dumps(payload)
    signature = "sha256=" + hmac.new(b"test-webhook-secret", body.encode(), hashlib.sha256).hexdigest()
    return _WebhookRequest(
        body,
        {
            "x-hub-signature-256": signature,
            "x-github-event": "issue_comment",
            "content-type": "application/json",
        },
    )


class TestGitHubBotUserIdEnv:
    """``config.botUserId ?? GITHUB_BOT_USER_ID`` (describe("createGitHubAdapter"))."""

    def test_auto_detects_bot_user_id_from_the_github_bot_user_id_env_var(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "env-secret")
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        monkeypatch.setenv("GITHUB_BOT_USER_ID", "4242")
        adapter = create_github_adapter()
        assert adapter.bot_user_id == "4242"
        assert adapter._bot_user_id == 4242

    def test_prefers_an_explicit_bot_user_id_over_github_bot_user_id(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "env-secret")
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        monkeypatch.setenv("GITHUB_BOT_USER_ID", "4242")
        adapter = create_github_adapter({"bot_user_id": 99})
        assert adapter.bot_user_id == "99"

    def test_explicit_zero_bot_user_id_still_wins_over_env(self, monkeypatch: pytest.MonkeyPatch):
        # ``??`` not ``||``: a falsy explicit id must not fall through to the
        # env var, and the property must not hide it behind a truthiness check.
        monkeypatch.setenv("GITHUB_BOT_USER_ID", "4242")
        adapter = _make_adapter(bot_user_id=0)
        assert adapter._bot_user_id == 0
        assert adapter.bot_user_id == "0"

    @pytest.mark.parametrize(
        "value",
        [
            "12abc",
            "0x10",
            "1_000",
            "4.2",
            pytest.param("\u0664\u0662", id="arabic-indic-digits"),
            "  ",
            pytest.param("9" * 5000, id="5000-digits"),
        ],
    )
    def test_malformed_env_value_is_ignored_with_a_warning(self, monkeypatch: pytest.MonkeyPatch, value: str):
        # Divergence from upstream: parseInt("12abc", 10) === 12 there. We
        # refuse to truncate and treat the value as unset.
        monkeypatch.setenv("GITHUB_BOT_USER_ID", value)
        logger = MockLogger()
        adapter = _make_adapter(logger=logger)
        assert adapter._bot_user_id is None
        assert adapter.bot_user_id is None
        assert logger.warn.calls == [
            ("Ignoring GITHUB_BOT_USER_ID: not a base-10 integer", {"length": len(value)}),
        ]

    def test_empty_env_value_is_treated_as_unset_without_a_warning(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GITHUB_BOT_USER_ID", "")
        logger = MockLogger()
        adapter = _make_adapter(logger=logger)
        assert adapter._bot_user_id is None
        assert logger.warn.calls == []

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(" 4242\n", 4242, id="surrounding-whitespace"),
            # ``parseInt("+4242", 10) === 4242``: the optional sign is parity.
            pytest.param("+4242", 4242, id="leading-plus"),
        ],
    )
    def test_whole_integer_env_value_is_accepted(self, monkeypatch: pytest.MonkeyPatch, value: str, expected: int):
        monkeypatch.setenv("GITHUB_BOT_USER_ID", value)
        logger = MockLogger()
        adapter = _make_adapter(logger=logger)
        assert adapter._bot_user_id == expected
        assert logger.warn.calls == []

    @pytest.mark.asyncio
    async def test_env_bot_user_id_skips_auto_detection(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GITHUB_BOT_USER_ID", "4242")
        adapter = _make_adapter()
        api = AsyncMock(side_effect=AssertionError("no API call expected"))
        adapter._github_api_request = api
        await adapter.initialize(MagicMock())
        api.assert_not_awaited()
        assert adapter._bot_user_id == 4242


class TestGitHubCaptureBotUserId:
    """``captureBotUserId``: describe("GitHubAdapter - Vercel Connect mode"), ported without Connect."""

    @staticmethod
    async def _adapter_with_failed_detection() -> tuple[GitHubAdapter, AsyncMock]:
        adapter = _make_adapter()
        api = AsyncMock(side_effect=RuntimeError("403 Resource not accessible by integration"))
        adapter._github_api_request = api
        await adapter._detect_bot_user_id()
        assert adapter._bot_user_id is None
        api.side_effect = None
        api.reset_mock()
        return adapter, api

    @pytest.mark.asyncio
    async def test_learns_the_bot_user_id_from_the_first_posted_comment(self):
        adapter, api = await self._adapter_with_failed_detection()
        logger = MockLogger()
        adapter._logger = logger
        assert adapter.bot_user_id is None

        api.return_value = _posted_comment({"id": 4242, "login": "bot[bot]", "type": "Bot"})
        result = await adapter.post_message("github:acme/app:issue:42", "hi")

        assert result.id == "100"
        assert api.await_args.args[:2] == ("POST", "/repos/acme/app/issues/42/comments")
        assert adapter._bot_user_id == 4242
        assert adapter.bot_user_id == "4242"
        assert logger.info.calls == [
            ("GitHub bot user ID learned from posted comment", {"botUserId": 4242, "login": "bot[bot]"}),
        ]

    @pytest.mark.asyncio
    async def test_review_comment_reply_branch_captures_the_bot_user_id(self):
        adapter, api = await self._adapter_with_failed_detection()
        api.return_value = _posted_comment({"id": 4343, "login": "bot[bot]"})
        await adapter.post_message("github:acme/app:42:rc:777", "hi")

        assert api.await_args.args[:2] == ("POST", "/repos/acme/app/pulls/42/comments/777/replies")
        assert adapter._bot_user_id == 4343

    @pytest.mark.asyncio
    async def test_capture_does_not_overwrite_a_known_bot_user_id(self):
        adapter = _make_adapter(bot_user_id=1)
        adapter._github_api_request = AsyncMock(return_value=_posted_comment({"id": 4242, "login": "other"}))
        await adapter.post_message("github:acme/app:42", "hi")
        assert adapter._bot_user_id == 1

    @pytest.mark.asyncio
    async def test_edit_message_does_not_capture(self):
        adapter, api = await self._adapter_with_failed_detection()
        api.return_value = _posted_comment({"id": 4242, "login": "bot[bot]"})
        await adapter.edit_message("github:acme/app:42", "100", "edited")
        assert api.await_args.args[0] == "PATCH"
        assert adapter._bot_user_id is None

    @pytest.mark.parametrize(
        "user",
        [None, "bot[bot]", {"login": "bot[bot]"}, {"id": "4242"}, {"id": True}, {"id": 4242.0}],
    )
    def test_ignores_users_without_a_numeric_id(self, user: object):
        adapter = _make_adapter()
        adapter._capture_bot_user_id(user)
        assert adapter._bot_user_id is None

    @pytest.mark.asyncio
    async def test_own_comment_webhook_is_skipped_after_capture(self):
        adapter, api = await self._adapter_with_failed_detection()
        chat = MagicMock()
        chat.process_message = MagicMock()
        adapter._chat = chat

        api.return_value = _posted_comment({"id": 4242, "login": "bot[bot]", "type": "Bot"})
        await adapter.post_message("github:acme/app:issue:42", "hi")
        api.reset_mock()

        response = await adapter.handle_webhook(_signed_issue_comment_request(sender_id=4242))

        assert response["status"] == 200
        chat.process_message.assert_not_called()
        # The id is known now, so the webhook does not retry detection either.
        api.assert_not_awaited()

        await adapter.handle_webhook(_signed_issue_comment_request(sender_id=7))
        chat.process_message.assert_called_once()
        assert chat.process_message.call_args.args[2].author.is_me is False
