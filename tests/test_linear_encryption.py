"""Tests for at-rest AES-256-GCM encryption of Linear OAuth tokens.

Port of the Linear slice of upstream ``9824d33`` (PR #441 adapter-hardening):
``encryptInstallation`` / ``decryptInstallation`` / ``maybeDecrypt`` in
packages/adapter-linear/src/index.ts, with plaintext-tolerance for a
zero-downtime rollout.
"""

from __future__ import annotations

import base64
import os

import pytest
from cryptography.exceptions import InvalidTag

from chat_sdk.adapters.linear.adapter import LinearAdapter
from chat_sdk.adapters.linear.types import (
    LinearAdapterAppConfig,
    LinearInstallation,
)
from chat_sdk.shared.errors import ValidationError
from chat_sdk.state.memory import MemoryStateAdapter

WEBHOOK_SECRET = "test-webhook-secret"
KEY_BYTES = os.urandom(32)
KEY_B64 = base64.b64encode(KEY_BYTES).decode("ascii")
KEY_HEX = KEY_BYTES.hex()


async def _make_adapter(encryption_key: str | None = None) -> LinearAdapter:
    """Create an initialized multi-tenant adapter wired to a MemoryState."""
    adapter = LinearAdapter(
        LinearAdapterAppConfig(
            client_id="cid",
            client_secret="csecret",
            webhook_secret=WEBHOOK_SECRET,
            encryption_key=encryption_key,
        )
    )
    state = MemoryStateAdapter()
    await state.connect()

    class _Chat:
        def get_state(self):
            return state

    adapter._chat = _Chat()  # type: ignore[assignment]
    return adapter


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


class TestEncryptionKeyResolution:
    def test_no_key_leaves_encryption_disabled(self):
        adapter = LinearAdapter(LinearAdapterAppConfig(client_id="c", client_secret="s", webhook_secret=WEBHOOK_SECRET))
        assert adapter._encryption_key is None

    def test_base64_key_decoded(self):
        adapter = LinearAdapter(
            LinearAdapterAppConfig(
                client_id="c", client_secret="s", webhook_secret=WEBHOOK_SECRET, encryption_key=KEY_B64
            )
        )
        assert adapter._encryption_key == KEY_BYTES

    def test_hex_key_decoded(self):
        adapter = LinearAdapter(
            LinearAdapterAppConfig(
                client_id="c", client_secret="s", webhook_secret=WEBHOOK_SECRET, encryption_key=KEY_HEX
            )
        )
        assert adapter._encryption_key == KEY_BYTES

    def test_env_fallback_used_when_config_unset(self, monkeypatch):
        monkeypatch.setenv("LINEAR_ENCRYPTION_KEY", KEY_B64)
        adapter = LinearAdapter(LinearAdapterAppConfig(client_id="c", client_secret="s", webhook_secret=WEBHOOK_SECRET))
        assert adapter._encryption_key == KEY_BYTES

    def test_explicit_empty_string_opts_out_of_env(self, monkeypatch):
        # An explicit empty key means "opted out"; the env var must NOT shadow
        # it (distinguish unset from empty -- truthiness-trap hazard).
        monkeypatch.setenv("LINEAR_ENCRYPTION_KEY", KEY_B64)
        adapter = LinearAdapter(
            LinearAdapterAppConfig(client_id="c", client_secret="s", webhook_secret=WEBHOOK_SECRET, encryption_key="")
        )
        assert adapter._encryption_key is None

    def test_explicit_empty_installation_key_prefix_is_honored(self):
        # An explicit empty prefix ("") is a valid caller choice and must NOT
        # be silently overridden by the default. A truthiness fallback
        # (``... or "linear:installation"``) would discard "" and yield the
        # default; an explicit ``is not None`` check preserves it.
        adapter = LinearAdapter(
            LinearAdapterAppConfig(
                client_id="c",
                client_secret="s",
                webhook_secret=WEBHOOK_SECRET,
                installation_key_prefix="",
            )
        )
        assert adapter._installation_key_prefix == ""


# ---------------------------------------------------------------------------
# Round-trip and storage shape
# ---------------------------------------------------------------------------


class TestEncryptRoundTrip:
    @pytest.mark.asyncio
    async def test_round_trip_yields_original_tokens(self):
        adapter = await _make_adapter(encryption_key=KEY_B64)
        await adapter.set_installation(
            "org-1",
            LinearInstallation(
                access_token="lin_oauth_access_abc",
                organization_id="org-1",
                bot_user_id="bot-1",
                expires_at=1234,
                refresh_token="lin_oauth_refresh_xyz",
            ),
        )

        loaded = await adapter.get_installation("org-1")
        assert loaded is not None
        assert loaded.access_token == "lin_oauth_access_abc"
        assert loaded.refresh_token == "lin_oauth_refresh_xyz"
        assert loaded.organization_id == "org-1"
        assert loaded.bot_user_id == "bot-1"
        assert loaded.expires_at == 1234

    @pytest.mark.asyncio
    async def test_tokens_are_encrypted_in_state(self):
        adapter = await _make_adapter(encryption_key=KEY_B64)
        await adapter.set_installation(
            "org-2",
            LinearInstallation(access_token="secret-access", organization_id="org-2"),
        )

        raw = await adapter._chat.get_state().get("linear:installation:org-2")
        # Stored as an encrypted envelope, not the plaintext token.
        assert isinstance(raw["accessToken"], dict)
        assert set(raw["accessToken"]) == {"iv", "data", "tag"}
        assert "secret-access" not in str(raw["accessToken"])
        # No refresh token supplied -> field omitted.
        assert "refreshToken" not in raw

    @pytest.mark.asyncio
    async def test_no_key_stores_plaintext(self):
        adapter = await _make_adapter(encryption_key=None)
        await adapter.set_installation(
            "org-3",
            LinearInstallation(access_token="plain-access", organization_id="org-3"),
        )
        raw = await adapter._chat.get_state().get("linear:installation:org-3")
        assert raw["accessToken"] == "plain-access"

        loaded = await adapter.get_installation("org-3")
        assert loaded is not None
        assert loaded.access_token == "plain-access"

    @pytest.mark.asyncio
    async def test_nonce_uniqueness_across_encryptions(self):
        adapter = await _make_adapter(encryption_key=KEY_B64)
        await adapter.set_installation("org-a", LinearInstallation(access_token="same-token", organization_id="org-a"))
        await adapter.set_installation("org-b", LinearInstallation(access_token="same-token", organization_id="org-b"))
        raw_a = await adapter._chat.get_state().get("linear:installation:org-a")
        raw_b = await adapter._chat.get_state().get("linear:installation:org-b")
        # Same plaintext, fresh random nonce each time -> distinct iv + ciphertext.
        assert raw_a["accessToken"]["iv"] != raw_b["accessToken"]["iv"]
        assert raw_a["accessToken"]["data"] != raw_b["accessToken"]["data"]


# ---------------------------------------------------------------------------
# Plaintext tolerance (zero-downtime rollout)
# ---------------------------------------------------------------------------


class TestPlaintextTolerance:
    @pytest.mark.asyncio
    async def test_plaintext_record_read_with_key_configured(self):
        # Simulate a record written BEFORE encryption was enabled, then read
        # back after a key was rotated in. It must decrypt-read as-is.
        adapter = await _make_adapter(encryption_key=KEY_B64)
        await adapter._chat.get_state().set(
            "linear:installation:legacy",
            {
                "accessToken": "legacy-plaintext-access",
                "refreshToken": "legacy-plaintext-refresh",
                "organizationId": "legacy",
                "botUserId": "bot-legacy",
                "expiresAt": 999,
            },
        )
        loaded = await adapter.get_installation("legacy")
        assert loaded is not None
        assert loaded.access_token == "legacy-plaintext-access"
        assert loaded.refresh_token == "legacy-plaintext-refresh"
        assert loaded.bot_user_id == "bot-legacy"
        assert loaded.expires_at == 999

    @pytest.mark.asyncio
    async def test_missing_installation_returns_none(self):
        adapter = await _make_adapter(encryption_key=KEY_B64)
        assert await adapter.get_installation("does-not-exist") is None

    @pytest.mark.asyncio
    async def test_delete_removes_installation(self):
        adapter = await _make_adapter(encryption_key=KEY_B64)
        await adapter.set_installation("org-del", LinearInstallation(access_token="tok", organization_id="org-del"))
        assert await adapter.get_installation("org-del") is not None
        await adapter.delete_installation("org-del")
        assert await adapter.get_installation("org-del") is None


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestEncryptionErrors:
    @pytest.mark.asyncio
    async def test_encrypted_record_without_key_raises_clear_error(self):
        # Write encrypted, then read with a fresh adapter that has NO key.
        writer = await _make_adapter(encryption_key=KEY_B64)
        await writer.set_installation(
            "org-enc", LinearInstallation(access_token="enc-token", organization_id="org-enc")
        )
        stored = await writer._chat.get_state().get("linear:installation:org-enc")

        reader = await _make_adapter(encryption_key=None)
        await reader._chat.get_state().set("linear:installation:org-enc", stored)
        with pytest.raises(ValidationError, match="encrypted but no encryption_key"):
            await reader.get_installation("org-enc")

    @pytest.mark.asyncio
    async def test_wrong_key_fails_loudly(self):
        writer = await _make_adapter(encryption_key=KEY_B64)
        await writer.set_installation(
            "org-wrong", LinearInstallation(access_token="enc-token", organization_id="org-wrong")
        )
        stored = await writer._chat.get_state().get("linear:installation:org-wrong")

        wrong_key = base64.b64encode(os.urandom(32)).decode("ascii")
        reader = await _make_adapter(encryption_key=wrong_key)
        await reader._chat.get_state().set("linear:installation:org-wrong", stored)
        # GCM auth-tag verification fails loudly -- never returns silent garbage.
        with pytest.raises(InvalidTag):
            await reader.get_installation("org-wrong")

    @pytest.mark.asyncio
    async def test_set_installation_before_initialize_raises(self):
        adapter = LinearAdapter(LinearAdapterAppConfig(client_id="c", client_secret="s", webhook_secret=WEBHOOK_SECRET))
        with pytest.raises(ValidationError, match="not initialized"):
            await adapter.set_installation("o", LinearInstallation(access_token="t", organization_id="o"))
