"""Google Workspace Events API integration for receiving all messages in a space.

By default, Google Chat only sends webhooks for @mentions. To receive ALL messages
in a space, you need to create a Workspace Events subscription that publishes to
a Pub/Sub topic, which then pushes to your webhook endpoint.

Setup flow:
1. Create a Pub/Sub topic in your GCP project
2. Create a Pub/Sub push subscription pointing to your /api/webhooks/gchat/pubsub endpoint
3. Call create_space_subscription() to subscribe to message events for a space
4. Handle Pub/Sub messages in your webhook with handle_pub_sub_message()

Python port of workspace-events.ts.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any

from chat_sdk.adapters.google_chat.types import ServiceAccountCredentials

# =============================================================================
# Types
# =============================================================================


@dataclass
class CreateSpaceSubscriptionOptions:
    """Options for creating a space subscription."""

    # The Pub/Sub topic to receive events (e.g., "projects/my-project/topics/my-topic")
    pubsub_topic: str
    # The space name (e.g., "spaces/AAAA...")
    space_name: str
    # Optional TTL for the subscription in seconds (default: 1 day, max: 1 day for Chat)
    ttl_seconds: int = 86400


@dataclass
class SpaceSubscriptionResult:
    """Result of creating a space subscription."""

    # The subscription resource name
    name: str
    # When the subscription expires (ISO 8601)
    expire_time: str


@dataclass
class PubSubPushMessage:
    """Pub/Sub push message wrapper (what Google sends to your endpoint)."""

    message: dict[str, Any]
    subscription: str


@dataclass
class GoogleChatReaction:
    """Google Chat reaction data."""

    # The emoji
    emoji: dict[str, Any] | None = None
    # Reaction resource name
    name: str = ""
    # The user who added/removed the reaction
    user: dict[str, Any] | None = None


@dataclass
class WorkspaceEventNotification:
    """Decoded Workspace Events notification payload."""

    # When the event occurred
    event_time: str = ""
    # Event type (e.g., "google.workspace.chat.message.v1.created")
    event_type: str = ""
    # Present for message.created events
    message: dict[str, Any] | None = None
    # Present for reaction.created/deleted events
    reaction: dict[str, Any] | None = None
    # Space info
    space: dict[str, Any] | None = None
    # The subscription that triggered this event
    subscription: str = ""
    # The resource being watched (e.g., "//chat.googleapis.com/spaces/AAAA")
    target_resource: str = ""


@dataclass
class WorkspaceEventsAuthCredentials:
    """Auth using service account credentials."""

    credentials: ServiceAccountCredentials
    impersonate_user: str | None = None


@dataclass
class WorkspaceEventsAuthADC:
    """Auth using Application Default Credentials."""

    use_application_default_credentials: bool = True
    impersonate_user: str | None = None


# Union type for auth options
WorkspaceEventsAuthOptions = WorkspaceEventsAuthCredentials | WorkspaceEventsAuthADC | dict[str, Any]


# =============================================================================
# Functions
# =============================================================================


async def create_space_subscription(
    options: CreateSpaceSubscriptionOptions,
    auth: WorkspaceEventsAuthOptions,
    http_session: Any | None = None,
) -> SpaceSubscriptionResult:
    """Create a Workspace Events subscription to receive all messages in a Chat space.

    Prerequisites:
    - Enable the "Google Workspace Events API" in your GCP project
    - Create a Pub/Sub topic and grant the Chat service account publish permissions
    - The calling user/service account needs permission to access the space

    Example::

        result = await create_space_subscription(
            CreateSpaceSubscriptionOptions(
                space_name="spaces/AAAAxxxxxx",
                pubsub_topic="projects/my-project/topics/chat-events",
            ),
            WorkspaceEventsAuthCredentials(
                credentials=ServiceAccountCredentials(
                    client_email="...",
                    private_key="...",
                ),
            ),
        )
    """

    access_token = await _get_access_token(
        auth,
        scopes=[
            "https://www.googleapis.com/auth/chat.spaces.readonly",
            "https://www.googleapis.com/auth/chat.messages.readonly",
        ],
        http_session=http_session,
    )

    space_name = options.space_name
    pubsub_topic = options.pubsub_topic
    ttl_seconds = options.ttl_seconds

    request_body = {
        "targetResource": f"//chat.googleapis.com/{space_name}",
        "eventTypes": [
            "google.workspace.chat.message.v1.created",
            "google.workspace.chat.message.v1.updated",
            "google.workspace.chat.reaction.v1.created",
            "google.workspace.chat.reaction.v1.deleted",
        ],
        "notificationEndpoint": {
            "pubsubTopic": pubsub_topic,
        },
        "payloadOptions": {
            "includeResource": True,
        },
        "ttl": f"{ttl_seconds}s",
    }

    url = "https://workspaceevents.googleapis.com/v1/subscriptions"

    session = http_session or await _make_session()
    try:
        async with session.post(
            url,
            json=request_body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        ) as response:
            response.raise_for_status()
            operation = await response.json()
    finally:
        if not http_session:
            await session.close()

    # The create operation returns a long-running operation
    if operation.get("done") and operation.get("response"):
        subscription = operation["response"]
        return SpaceSubscriptionResult(
            name=subscription.get("name", ""),
            expire_time=subscription.get("expireTime", ""),
        )

    # Operation is still pending - return operation name
    return SpaceSubscriptionResult(
        name=operation.get("name", "pending"),
        expire_time="",
    )


async def list_space_subscriptions(
    space_name: str,
    auth: WorkspaceEventsAuthOptions,
    http_session: Any | None = None,
) -> list[dict[str, Any]]:
    """List active subscriptions for a target resource."""
    access_token = await _get_access_token(
        auth,
        scopes=[
            "https://www.googleapis.com/auth/chat.spaces.readonly",
        ],
        http_session=http_session,
    )

    target_resource = f"//chat.googleapis.com/{space_name}"
    url = f'https://workspaceevents.googleapis.com/v1/subscriptions?filter=target_resource="{target_resource}"'

    session = http_session or await _make_session()
    try:
        async with session.get(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
            },
        ) as response:
            response.raise_for_status()
            data = await response.json()
    finally:
        if not http_session:
            await session.close()

    return [
        {
            "name": sub.get("name", ""),
            "expire_time": sub.get("expireTime", ""),
            "event_types": sub.get("eventTypes", []),
        }
        for sub in data.get("subscriptions", [])
    ]


async def delete_space_subscription(
    subscription_name: str,
    auth: WorkspaceEventsAuthOptions,
    http_session: Any | None = None,
) -> None:
    """Delete a Workspace Events subscription."""
    access_token = await _get_access_token(
        auth,
        scopes=[
            "https://www.googleapis.com/auth/chat.spaces.readonly",
        ],
        http_session=http_session,
    )

    url = f"https://workspaceevents.googleapis.com/v1/{subscription_name}"

    session = http_session or await _make_session()
    try:
        async with session.delete(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
            },
        ) as response:
            response.raise_for_status()
    finally:
        if not http_session:
            await session.close()


def decode_pub_sub_message(
    push_message: dict[str, Any],
) -> WorkspaceEventNotification:
    """Decode a Pub/Sub push message into a Workspace Event notification.

    The message uses CloudEvents format where event metadata is in attributes
    (ce-type, ce-source, ce-subject, ce-time) and the payload is base64 encoded.

    Example::

        # In your /api/webhooks/gchat/pubsub route:
        body = await request.json()
        event = decode_pub_sub_message(body)

        if event.event_type == "google.workspace.chat.message.v1.created":
            # Handle new message
            print("New message:", event.message.get("text"))
    """
    # Decode the base64 payload
    message_data = push_message.get("message", {})
    data_b64 = message_data.get("data", "")
    data_bytes = base64.b64decode(data_b64)
    payload = json.loads(data_bytes.decode("utf-8"))

    # Extract CloudEvents metadata from attributes
    attributes = message_data.get("attributes", {})

    return WorkspaceEventNotification(
        subscription=push_message.get("subscription", ""),
        target_resource=attributes.get("ce-subject", ""),
        event_type=attributes.get("ce-type", ""),
        event_time=attributes.get("ce-time", "") or message_data.get("publishTime", ""),
        message=payload.get("message"),
        reaction=payload.get("reaction"),
    )


# =============================================================================
# Internal helpers
# =============================================================================


async def _make_session() -> Any:
    """Create a new aiohttp.ClientSession (used when no shared session is provided)."""
    import aiohttp

    return aiohttp.ClientSession()


async def _get_access_token(
    auth: WorkspaceEventsAuthOptions,
    scopes: list[str],
    http_session: Any | None = None,
) -> str:
    """Get an access token for Google API calls.

    Supports service account credentials and ADC.
    """
    if isinstance(auth, WorkspaceEventsAuthCredentials):
        return await _get_service_account_token(auth.credentials, scopes, auth.impersonate_user, http_session)
    elif isinstance(auth, WorkspaceEventsAuthADC):
        return await _get_adc_token(scopes, http_session)
    elif isinstance(auth, dict) and "auth" in auth:
        # Custom auth - assume it provides a token directly
        custom_auth = auth["auth"]
        if callable(custom_auth):
            auth_fn: Any = custom_auth
            return await auth_fn()
        return str(custom_auth)
    else:
        raise ValueError("Unsupported auth type for workspace events")


async def _get_service_account_token(
    credentials: ServiceAccountCredentials,
    scopes: list[str],
    impersonate_user: str | None = None,
    http_session: Any | None = None,
) -> str:
    """Get an access token using service account credentials (JWT flow)."""
    import time

    # Lazy import for jwt
    import jwt as pyjwt

    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": credentials.client_email,
        "scope": " ".join(scopes),
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }

    if impersonate_user:
        claims["sub"] = impersonate_user

    token = pyjwt.encode(
        claims,
        credentials.private_key,
        algorithm="RS256",
    )

    session = http_session or await _make_session()
    try:
        async with session.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": token,
            },
        ) as response:
            response.raise_for_status()
            data = await response.json()
            return data["access_token"]
    finally:
        if not http_session:
            await session.close()


async def _get_adc_token(scopes: list[str], http_session: Any | None = None) -> str:
    """Get an access token using Application Default Credentials.

    Uses google-auth library if available, otherwise falls back
    to metadata server for GCE/Cloud Run environments.
    """
    try:
        # google-auth is an optional dependency; pyrefly cannot see it.
        import google.auth  # pyrefly: ignore[missing-import]
        import google.auth.transport.requests  # pyrefly: ignore[missing-import]

        creds, _ = google.auth.default(scopes=scopes)
        request = google.auth.transport.requests.Request()
        await asyncio.to_thread(creds.refresh, request)
        return creds.token
    except ImportError:
        pass

    # Fallback: metadata server (GCE, Cloud Run, etc.)
    url = f"http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token?scopes={','.join(scopes)}"

    session = http_session or await _make_session()
    try:
        async with session.get(
            url,
            headers={"Metadata-Flavor": "Google"},
        ) as response:
            response.raise_for_status()
            data = await response.json()
            return data["access_token"]
    finally:
        if not http_session:
            await session.close()
