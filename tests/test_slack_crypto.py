"""Tests for Slack token encryption/decryption utilities.

Port of packages/adapter-slack/src/crypto.test.ts.
"""

from __future__ import annotations

import base64
import os

import pytest

from chat_sdk.adapters.slack.crypto import (
    EncryptedTokenData,
    decode_key,
    decrypt_token,
    encrypt_token,
    is_encrypted_token_data,
)

TEST_KEY = os.urandom(32)
TEST_KEY_BASE64 = base64.b64encode(TEST_KEY).decode("ascii")
TEST_KEY_HEX = TEST_KEY.hex()


# ---------------------------------------------------------------------------
# encryptToken / decryptToken
# ---------------------------------------------------------------------------


class TestEncryptDecrypt:
    def test_round_trips_correctly(self):
        token = "xoxb-test-bot-token-12345"
        encrypted = encrypt_token(token, TEST_KEY)
        decrypted = decrypt_token(encrypted, TEST_KEY)
        assert decrypted == token

    def test_produces_different_ciphertexts_random_iv(self):
        token = "xoxb-same-token"
        a = encrypt_token(token, TEST_KEY)
        b = encrypt_token(token, TEST_KEY)
        assert a.data != b.data
        assert a.iv != b.iv

    def test_wrong_key_raises(self):
        token = "xoxb-secret"
        encrypted = encrypt_token(token, TEST_KEY)
        wrong_key = os.urandom(32)
        with pytest.raises(Exception):
            decrypt_token(encrypted, wrong_key)

    def test_tampered_ciphertext_raises(self):
        token = "xoxb-secret"
        encrypted = encrypt_token(token, TEST_KEY)
        encrypted.data = base64.b64encode(b"tampered").decode("ascii")
        with pytest.raises(Exception):
            decrypt_token(encrypted, TEST_KEY)


# ---------------------------------------------------------------------------
# decodeKey
# ---------------------------------------------------------------------------


class TestDecodeKey:
    def test_decodes_base64_key(self):
        key = decode_key(TEST_KEY_BASE64)
        assert len(key) == 32
        assert key == TEST_KEY

    def test_decodes_hex_key(self):
        key = decode_key(TEST_KEY_HEX)
        assert len(key) == 32
        assert key == TEST_KEY

    def test_trims_whitespace(self):
        key = decode_key(f"  {TEST_KEY_BASE64}  ")
        assert len(key) == 32

    def test_non_32_byte_key_raises(self):
        short_key = base64.b64encode(os.urandom(16)).decode("ascii")
        with pytest.raises(ValueError, match="32 bytes"):
            decode_key(short_key)

    def test_empty_string_raises(self):
        with pytest.raises(Exception):
            decode_key("")


# ---------------------------------------------------------------------------
# isEncryptedTokenData
# ---------------------------------------------------------------------------


class TestIsEncryptedTokenData:
    def test_true_for_encrypted_data(self):
        encrypted = encrypt_token("test", TEST_KEY)
        assert is_encrypted_token_data(encrypted.__dict__) is True

    def test_false_for_plain_string(self):
        assert is_encrypted_token_data("xoxb-token") is False

    def test_false_for_none(self):
        assert is_encrypted_token_data(None) is False

    def test_false_for_missing_fields(self):
        assert is_encrypted_token_data({"iv": "a", "data": "b"}) is False
        assert is_encrypted_token_data({"iv": "a", "tag": "c"}) is False

    def test_false_for_non_string_fields(self):
        assert is_encrypted_token_data({"iv": 1, "data": 2, "tag": 3}) is False
