"""Tests for Google Chat Workspace Events -- targeting 80%+ coverage.

Ported from packages/adapter-gchat/src/workspace-events.test.ts.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat_sdk.adapters.google_chat.types import ServiceAccountCredentials
from chat_sdk.adapters.google_chat.workspace_events import (
    CreateSpaceSubscriptionOptions,
    WorkspaceEventsAuthCredentials,
    create_space_subscription,
    decode_pub_sub_message,
    delete_space_subscription,
    list_space_subscriptions,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_credentials() -> ServiceAccountCredentials:
    return ServiceAccountCredentials(
        client_email="bot@project.iam.gserviceaccount.com",
        private_key="-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
    )


def _make_pub_sub_message(
    payload: dict[str, Any],
    attributes: dict[str, str] | None = None,
    publish_time: str = "2024-01-15T10:00:00Z",
) -> dict[str, Any]:
    return {
        "message": {
            "data": base64.b64encode(json.dumps(payload).encode()).decode(),
            "messageId": "msg-123",
            "publishTime": publish_time,
            **({"attributes": attributes} if attributes else {}),
        },
        "subscription": "projects/my-project/subscriptions/my-sub",
    }


class _MockSession:
    """Mock aiohttp.ClientSession supporting the nested context-manager pattern
    used by the workspace_events functions:

        async with aiohttp.ClientSession() as session:
            async with session.post(url, ...) as response:
                ...
    """

    def __init__(self, response_data: Any, status: int = 200):
        self._response = MagicMock()
        self._response.status = status
        self._response.ok = status < 400
        self._response.raise_for_status = MagicMock()
        if status >= 400:
            self._response.raise_for_status.side_effect = Exception(f"HTTP {status}")
        self._response.json = AsyncMock(return_value=response_data)
        self._response.text = AsyncMock(
            return_value=json.dumps(response_data) if isinstance(response_data, dict) else str(response_data)
        )

        self.post_calls: list[tuple] = []
        self.get_calls: list[tuple] = []
        self.delete_calls: list[tuple] = []

    def _make_method_cm(self) -> MagicMock:
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=self._response)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    def post(self, url: str, **kwargs) -> MagicMock:
        self.post_calls.append((url, kwargs))
        return self._make_method_cm()

    def get(self, url: str, **kwargs) -> MagicMock:
        self.get_calls.append((url, kwargs))
        return self._make_method_cm()

    def delete(self, url: str, **kwargs) -> MagicMock:
        self.delete_calls.append((url, kwargs))
        return self._make_method_cm()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# decode_pub_sub_message
# ---------------------------------------------------------------------------


class TestDecodePubSubMessage:
    def test_decodes_base64_message_payload(self):
        push = _make_pub_sub_message({"message": {"text": "Hello world", "name": "spaces/ABC/messages/123"}})
        result = decode_pub_sub_message(push)
        assert result.message is not None
        assert result.message["text"] == "Hello world"
        assert result.subscription == "projects/my-project/subscriptions/my-sub"

    def test_extracts_cloud_events_attributes(self):
        push = _make_pub_sub_message(
            {"message": {"text": "test"}},
            attributes={
                "ce-type": "google.workspace.chat.message.v1.created",
                "ce-subject": "//chat.googleapis.com/spaces/ABC",
                "ce-time": "2024-01-15T10:00:00Z",
            },
        )
        result = decode_pub_sub_message(push)
        assert result.event_type == "google.workspace.chat.message.v1.created"
        assert result.target_resource == "//chat.googleapis.com/spaces/ABC"
        assert result.event_time == "2024-01-15T10:00:00Z"

    def test_handles_missing_attributes(self):
        push = _make_pub_sub_message({"message": {"text": "test"}})
        result = decode_pub_sub_message(push)
        assert result.event_type == ""
        assert result.target_resource == ""
        # Falls back to publishTime
        assert result.event_time == "2024-01-15T10:00:00Z"

    def test_decodes_reaction_payload(self):
        push = _make_pub_sub_message(
            {
                "reaction": {
                    "name": "spaces/ABC/messages/123/reactions/456",
                    "emoji": {"unicode": "\U0001f44d"},
                },
            },
            attributes={"ce-type": "google.workspace.chat.reaction.v1.created"},
        )
        result = decode_pub_sub_message(push)
        assert result.reaction is not None
        assert result.reaction["name"] == "spaces/ABC/messages/123/reactions/456"
        assert result.reaction["emoji"]["unicode"] == "\U0001f44d"

    def test_empty_payload(self):
        push = _make_pub_sub_message({})
        result = decode_pub_sub_message(push)
        assert result.message is None
        assert result.reaction is None


# ---------------------------------------------------------------------------
# create_space_subscription
# ---------------------------------------------------------------------------


class TestCreateSpaceSubscription:
    async def test_returns_result_when_done(self):
        mock_session = _MockSession(
            {
                "done": True,
                "response": {
                    "name": "subscriptions/sub-abc123",
                    "expireTime": "2024-01-16T10:00:00Z",
                },
            }
        )

        with (
            patch(
                "chat_sdk.adapters.google_chat.workspace_events._get_access_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):
            result = await create_space_subscription(
                CreateSpaceSubscriptionOptions(
                    space_name="spaces/AAABBBCCC",
                    pubsub_topic="projects/my-project/topics/chat-events",
                ),
                WorkspaceEventsAuthCredentials(credentials=_make_credentials()),
            )
            assert result.name == "subscriptions/sub-abc123"
            assert result.expire_time == "2024-01-16T10:00:00Z"

    async def test_returns_pending_when_not_done(self):
        mock_session = _MockSession(
            {
                "done": False,
                "name": "operations/op-xyz",
            }
        )

        with (
            patch(
                "chat_sdk.adapters.google_chat.workspace_events._get_access_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):
            result = await create_space_subscription(
                CreateSpaceSubscriptionOptions(
                    space_name="spaces/AAABBBCCC",
                    pubsub_topic="projects/my-project/topics/chat-events",
                ),
                WorkspaceEventsAuthCredentials(credentials=_make_credentials()),
            )
            assert result.name == "operations/op-xyz"
            assert result.expire_time == ""

    async def test_request_body_contains_expected_fields(self):
        """Verify the subscription request body has the right structure."""
        mock_session = _MockSession({"done": False, "name": "op"})

        with (
            patch(
                "chat_sdk.adapters.google_chat.workspace_events._get_access_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):
            await create_space_subscription(
                CreateSpaceSubscriptionOptions(
                    space_name="spaces/ABC",
                    pubsub_topic="projects/proj/topics/topic",
                    ttl_seconds=3600,
                ),
                WorkspaceEventsAuthCredentials(credentials=_make_credentials()),
            )
            assert len(mock_session.post_calls) == 1
            url, kwargs = mock_session.post_calls[0]
            assert "workspaceevents.googleapis.com" in url
            json_body = kwargs["json"]
            assert json_body["targetResource"] == "//chat.googleapis.com/spaces/ABC"
            assert json_body["ttl"] == "3600s"
            assert "google.workspace.chat.message.v1.created" in json_body["eventTypes"]

    async def test_auth_header_contains_bearer_token(self):
        mock_session = _MockSession({"done": False, "name": "op"})

        with (
            patch(
                "chat_sdk.adapters.google_chat.workspace_events._get_access_token",
                new_callable=AsyncMock,
                return_value="my-bearer-token",
            ),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):
            await create_space_subscription(
                CreateSpaceSubscriptionOptions(
                    space_name="spaces/X",
                    pubsub_topic="projects/p/topics/t",
                ),
                WorkspaceEventsAuthCredentials(credentials=_make_credentials()),
            )
            _, kwargs = mock_session.post_calls[0]
            assert kwargs["headers"]["Authorization"] == "Bearer my-bearer-token"


# ---------------------------------------------------------------------------
# list_space_subscriptions
# ---------------------------------------------------------------------------


class TestListSpaceSubscriptions:
    async def test_returns_mapped_subscriptions(self):
        mock_session = _MockSession(
            {
                "subscriptions": [
                    {
                        "name": "subscriptions/sub-1",
                        "expireTime": "2024-01-16T10:00:00Z",
                        "eventTypes": [
                            "google.workspace.chat.message.v1.created",
                            "google.workspace.chat.message.v1.updated",
                        ],
                    },
                    {
                        "name": "subscriptions/sub-2",
                        "expireTime": "2024-01-17T10:00:00Z",
                        "eventTypes": ["google.workspace.chat.reaction.v1.created"],
                    },
                ]
            }
        )

        with (
            patch(
                "chat_sdk.adapters.google_chat.workspace_events._get_access_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):
            result = await list_space_subscriptions(
                "spaces/AAABBBCCC",
                WorkspaceEventsAuthCredentials(credentials=_make_credentials()),
            )
            assert len(result) == 2
            assert result[0]["name"] == "subscriptions/sub-1"
            assert result[0]["expire_time"] == "2024-01-16T10:00:00Z"
            assert result[0]["event_types"] == [
                "google.workspace.chat.message.v1.created",
                "google.workspace.chat.message.v1.updated",
            ]
            assert result[1]["name"] == "subscriptions/sub-2"

    async def test_returns_empty_when_no_subscriptions(self):
        mock_session = _MockSession({})

        with (
            patch(
                "chat_sdk.adapters.google_chat.workspace_events._get_access_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):
            result = await list_space_subscriptions(
                "spaces/AAABBBCCC",
                WorkspaceEventsAuthCredentials(credentials=_make_credentials()),
            )
            assert result == []

    async def test_filter_parameter_in_url(self):
        mock_session = _MockSession({"subscriptions": []})

        with (
            patch(
                "chat_sdk.adapters.google_chat.workspace_events._get_access_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):
            await list_space_subscriptions(
                "spaces/TEST",
                WorkspaceEventsAuthCredentials(credentials=_make_credentials()),
            )
            assert len(mock_session.get_calls) == 1
            url, _ = mock_session.get_calls[0]
            assert "filter" in url
            assert "//chat.googleapis.com/spaces/TEST" in url


# ---------------------------------------------------------------------------
# delete_space_subscription
# ---------------------------------------------------------------------------


class TestDeleteSpaceSubscription:
    async def test_calls_delete_with_correct_url(self):
        mock_session = _MockSession({})

        with (
            patch(
                "chat_sdk.adapters.google_chat.workspace_events._get_access_token",
                new_callable=AsyncMock,
                return_value="fake-token",
            ),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):
            await delete_space_subscription(
                "subscriptions/sub-abc123",
                WorkspaceEventsAuthCredentials(credentials=_make_credentials()),
            )
            assert len(mock_session.delete_calls) == 1
            url, _ = mock_session.delete_calls[0]
            assert "subscriptions/sub-abc123" in url

    async def test_delete_passes_auth_header(self):
        mock_session = _MockSession({})

        with (
            patch(
                "chat_sdk.adapters.google_chat.workspace_events._get_access_token",
                new_callable=AsyncMock,
                return_value="del-token",
            ),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):
            await delete_space_subscription(
                "subscriptions/sub-1",
                WorkspaceEventsAuthCredentials(credentials=_make_credentials()),
            )
            _, kwargs = mock_session.delete_calls[0]
            assert kwargs["headers"]["Authorization"] == "Bearer del-token"


# ---------------------------------------------------------------------------
# _get_access_token
# ---------------------------------------------------------------------------


class TestGetAccessToken:
    async def test_service_account_auth(self):
        """Service account credentials use JWT flow."""
        from chat_sdk.adapters.google_chat.workspace_events import _get_access_token

        mock_session = _MockSession({"access_token": "sa-token-123"})

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch(
                "jwt.encode",
                return_value="jwt-assertion",
            ),
        ):
            token = await _get_access_token(
                WorkspaceEventsAuthCredentials(credentials=_make_credentials()),
                scopes=["https://www.googleapis.com/auth/chat.spaces.readonly"],
            )
            assert token == "sa-token-123"

    async def test_custom_auth_callable(self):
        """Custom auth dict with callable returns token."""
        from chat_sdk.adapters.google_chat.workspace_events import _get_access_token

        async def custom_auth_fn():
            return "custom-token-abc"

        token = await _get_access_token(
            {"auth": custom_auth_fn},
            scopes=[],
        )
        assert token == "custom-token-abc"

    async def test_custom_auth_string(self):
        """Custom auth dict with string returns that string."""
        from chat_sdk.adapters.google_chat.workspace_events import _get_access_token

        token = await _get_access_token(
            {"auth": "static-token"},
            scopes=[],
        )
        assert token == "static-token"

    async def test_unsupported_auth_raises(self):
        from chat_sdk.adapters.google_chat.workspace_events import _get_access_token

        with pytest.raises(ValueError, match="Unsupported auth type"):
            await _get_access_token({"foo": "bar"}, scopes=[])

    async def test_service_account_with_impersonate(self):
        """JWT claims should include 'sub' when impersonating a user."""
        from chat_sdk.adapters.google_chat.workspace_events import _get_access_token

        mock_session = _MockSession({"access_token": "impersonated-token"})
        captured_claims = {}

        def mock_encode(claims, key, algorithm):
            captured_claims.update(claims)
            return "jwt-assertion"

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch(
                "jwt.encode",
                side_effect=mock_encode,
            ),
        ):
            token = await _get_access_token(
                WorkspaceEventsAuthCredentials(
                    credentials=_make_credentials(),
                    impersonate_user="admin@example.com",
                ),
                scopes=["https://www.googleapis.com/auth/chat.spaces.readonly"],
            )
            assert token == "impersonated-token"
            assert captured_claims.get("sub") == "admin@example.com"
