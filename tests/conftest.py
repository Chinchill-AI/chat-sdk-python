"""Test configuration and shared fixtures for chat-sdk."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

import pytest

from chat_sdk.state.memory import MemoryStateAdapter
from chat_sdk.testing import (
    MockAdapter,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# Env vars that change adapter behaviour when merely present in the shell.
# Cleared for every test so a developer or CI environment configured for a
# real deployment (for example GOOGLE_CHAT_BOT_USER_ID, which the Google Chat
# docs tell users to export) cannot flip "identity not configured" tests.
# Tests that need a value set it explicitly with ``monkeypatch.setenv``.
_AMBIENT_IDENTITY_ENV_KEYS = ("GOOGLE_CHAT_BOT_USER_ID",)


@pytest.fixture(autouse=True)
def _clear_ambient_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _AMBIENT_IDENTITY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_adapter() -> MockAdapter:
    """Create a fresh MockAdapter for each test."""
    return create_mock_adapter("slack")


@pytest.fixture
def mock_state() -> MockStateAdapter:
    """Create a fresh MockStateAdapter for each test."""
    return create_mock_state()


@pytest.fixture
async def memory_state() -> MemoryStateAdapter:
    """Create and connect a MemoryStateAdapter for each test."""
    state = MemoryStateAdapter()
    await state.connect()
    yield state  # type: ignore[misc]
    await state.disconnect()


# ---------------------------------------------------------------------------
# HTTP request helper
# ---------------------------------------------------------------------------


@dataclass
class FakeRequest:
    """Minimal request object for webhook testing."""

    method: str = "POST"
    url: str = "http://test.com"
    headers: dict[str, str] | None = None
    body: bytes | str | None = None


def make_request(
    method: str = "POST",
    url: str = "http://test.com",
    headers: dict[str, str] | None = None,
    body: bytes | str | None = None,
) -> FakeRequest:
    """Build a minimal fake HTTP request for webhook testing."""
    return FakeRequest(method=method, url=url, headers=headers, body=body)


# ---------------------------------------------------------------------------
# HMAC helper
# ---------------------------------------------------------------------------


def compute_hmac_sha256(secret: str | bytes, body: str | bytes) -> str:
    """Compute an HMAC-SHA256 hex digest for webhook signature verification."""
    if isinstance(secret, str):
        secret = secret.encode()
    if isinstance(body, str):
        body = body.encode()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()
