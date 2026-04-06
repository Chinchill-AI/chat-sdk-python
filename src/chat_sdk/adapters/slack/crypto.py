"""AES-256-GCM encryption utilities for Slack token storage.

Port of crypto.ts from the Vercel Chat SDK Slack adapter.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

ALGORITHM = "aes-256-gcm"
IV_LENGTH = 12
AUTH_TAG_LENGTH = 16
HEX_KEY_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass
class EncryptedTokenData:
    """Encrypted token components (base64-encoded)."""

    data: str
    iv: str
    tag: str


def encrypt_token(plaintext: str, key: bytes) -> EncryptedTokenData:
    """Encrypt a token using AES-256-GCM.

    Args:
        plaintext: The token to encrypt.
        key: 32-byte encryption key.

    Returns:
        EncryptedTokenData with base64-encoded iv, data, and tag.
    """
    import base64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    iv = os.urandom(IV_LENGTH)
    aesgcm = AESGCM(key)
    # AESGCM.encrypt returns ciphertext + tag concatenated
    ct_with_tag = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
    # Split into ciphertext and auth tag (last 16 bytes)
    ciphertext = ct_with_tag[:-AUTH_TAG_LENGTH]
    tag = ct_with_tag[-AUTH_TAG_LENGTH:]

    return EncryptedTokenData(
        iv=base64.b64encode(iv).decode("ascii"),
        data=base64.b64encode(ciphertext).decode("ascii"),
        tag=base64.b64encode(tag).decode("ascii"),
    )


def decrypt_token(encrypted: EncryptedTokenData, key: bytes) -> str:
    """Decrypt a token encrypted with AES-256-GCM.

    Args:
        encrypted: The encrypted token data.
        key: 32-byte encryption key.

    Returns:
        The decrypted plaintext token.
    """
    import base64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    iv = base64.b64decode(encrypted.iv)
    ciphertext = base64.b64decode(encrypted.data)
    tag = base64.b64decode(encrypted.tag)

    aesgcm = AESGCM(key)
    # AESGCM.decrypt expects ciphertext + tag concatenated
    plaintext = aesgcm.decrypt(iv, ciphertext + tag, None)
    return plaintext.decode("utf-8")


def is_encrypted_token_data(value: Any) -> bool:
    """Check if a value looks like EncryptedTokenData."""
    if not isinstance(value, dict):
        return False
    return isinstance(value.get("iv"), str) and isinstance(value.get("data"), str) and isinstance(value.get("tag"), str)


def decode_key(raw_key: str) -> bytes:
    """Decode a hex or base64 encoded encryption key to 32 bytes.

    Args:
        raw_key: 64-char hex string or 44-char base64 string.

    Returns:
        32-byte key.

    Raises:
        ValueError: If the key does not decode to exactly 32 bytes.
    """
    import base64

    trimmed = raw_key.strip()
    # Detect hex encoding: 64 hex chars = 32 bytes
    is_hex = bool(HEX_KEY_PATTERN.match(trimmed))
    key = bytes.fromhex(trimmed) if is_hex else base64.b64decode(trimmed)

    if len(key) != 32:
        raise ValueError(
            f"Encryption key must decode to exactly 32 bytes (received {len(key)}). "
            "Use a 64-char hex string or 44-char base64 string."
        )
    return key
