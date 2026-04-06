"""Shared fixtures and helpers for integration tests.

Provides ``create_chat`` for quick Chat construction, ``create_msg`` for
building test messages, and signature-computation helpers for verifying
webhook authentication flows.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

import pytest
from chat_sdk.chat import Chat
from chat_sdk.testing import (
    MockAdapter,
    MockLogger,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
)
from chat_sdk.state.memory import MemoryStateAdapter
from chat_sdk.types import (
    Author,
    ChatConfig,
    ConcurrencyConfig,
    ConcurrencyStrategy,
    Message,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def mock_adapter() -> MockAdapter:
    """Create a fresh MockAdapter named 'slack'."""
    return create_mock_adapter("slack")


@pytest.fixture
def mock_state() -> MockStateAdapter:
    """Create a fresh MockStateAdapter."""
    return create_mock_state()


@pytest.fixture
async def memory_state() -> MemoryStateAdapter:
    """Create and connect a MemoryStateAdapter for each test."""
    state = MemoryStateAdapter()
    await state.connect()
    yield state  # type: ignore[misc]
    await state.disconnect()


# ---------------------------------------------------------------------------
# Chat factory
# ---------------------------------------------------------------------------


async def create_chat(
    adapters: dict[str, MockAdapter] | None = None,
    state: MockStateAdapter | MemoryStateAdapter | None = None,
    concurrency: ConcurrencyStrategy | ConcurrencyConfig | None = None,
    user_name: str = "testbot",
    **overrides: Any,
) -> tuple[Chat, dict[str, MockAdapter], MockStateAdapter | MemoryStateAdapter]:
    """Create and initialize a Chat instance with the given adapters and state.

    Returns (chat, adapters_dict, state_adapter).
    """
    if adapters is None:
        adapters = {"slack": create_mock_adapter("slack")}
    if state is None:
        state = create_mock_state()

    config_kwargs: dict[str, Any] = {
        "user_name": user_name,
        "adapters": adapters,
        "state": state,
        "logger": MockLogger(),
    }
    if concurrency is not None:
        config_kwargs["concurrency"] = concurrency
    config_kwargs.update(overrides)

    chat = Chat(ChatConfig(**config_kwargs))
    # Trigger initialization via webhook on the first adapter
    first_adapter_name = next(iter(adapters))
    await chat.webhooks[first_adapter_name]("request")

    return chat, adapters, state


# ---------------------------------------------------------------------------
# Message factory
# ---------------------------------------------------------------------------

_msg_counter = 0


def create_msg(
    text: str,
    *,
    msg_id: str | None = None,
    thread_id: str = "slack:C123:1234.5678",
    user_id: str = "U123",
    user_name: str = "testuser",
    is_bot: bool = False,
    is_me: bool = False,
    is_mention: bool | None = None,
) -> Message:
    """Build a Message for testing with sensible defaults.

    Auto-generates a unique message ID if none is provided.
    """
    global _msg_counter
    _msg_counter += 1
    mid = msg_id or f"msg-{_msg_counter}"

    return create_test_message(
        mid,
        text,
        thread_id=thread_id,
        author=Author(
            user_id=user_id,
            user_name=user_name,
            full_name=user_name.title(),
            is_bot=is_bot,
            is_me=is_me,
        ),
        is_mention=is_mention,
    )


# ---------------------------------------------------------------------------
# Request / signature helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeWebhookRequest:
    """Minimal request object for webhook testing."""

    method: str = "POST"
    url: str = "http://test.com/webhook"
    headers: dict[str, str] | None = None
    body: bytes | str | None = None


def make_webhook_request(
    method: str = "POST",
    url: str = "http://test.com/webhook",
    headers: dict[str, str] | None = None,
    body: bytes | str | None = None,
) -> FakeWebhookRequest:
    """Build a minimal fake HTTP request for webhook testing."""
    return FakeWebhookRequest(method=method, url=url, headers=headers, body=body)


def compute_slack_signature(
    secret: str,
    body: str,
    timestamp: str | None = None,
) -> tuple[str, str]:
    """Compute a Slack-style v0 signature.

    Returns (signature, timestamp) so the caller can set both the
    ``X-Slack-Signature`` and ``X-Slack-Request-Timestamp`` headers.
    """
    ts = timestamp or str(int(time.time()))
    basestring = f"v0:{ts}:{body}"
    sig = (
        "v0="
        + hmac.new(
            secret.encode(),
            basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    return sig, ts


def compute_hmac_signature(
    secret: str | bytes,
    body: str | bytes,
    algorithm: str = "sha256",
) -> str:
    """Compute a generic HMAC hex digest for webhook signature verification."""
    if isinstance(secret, str):
        secret = secret.encode()
    if isinstance(body, str):
        body = body.encode()
    return hmac.new(secret, body, getattr(hashlib, algorithm)).hexdigest()
