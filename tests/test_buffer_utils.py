"""Tests for buffer_utils -- targeting 80%+ coverage.

Ported from packages/adapter-shared/src/buffer-utils.test.ts.
"""

from __future__ import annotations

import pytest

from chat_sdk.shared.buffer_utils import buffer_to_data_uri, to_buffer, to_buffer_sync

# ---------------------------------------------------------------------------
# to_buffer (async)
# ---------------------------------------------------------------------------


class TestToBuffer:
    async def test_bytes_passthrough(self):
        data = b"hello"
        result = await to_buffer(data, "slack")
        assert result == b"hello"
        assert isinstance(result, bytes)

    async def test_bytearray_conversion(self):
        data = bytearray(b"hello")
        result = await to_buffer(data, "slack")
        assert result == b"hello"
        assert isinstance(result, bytes)

    async def test_memoryview_conversion(self):
        data = memoryview(b"hello")
        result = await to_buffer(data, "slack")
        assert result == b"hello"
        assert isinstance(result, bytes)

    async def test_raises_for_unsupported_type_string(self):
        from chat_sdk.shared.errors import ValidationError

        with pytest.raises(ValidationError):
            await to_buffer("string", "slack")

    async def test_raises_for_unsupported_type_int(self):
        from chat_sdk.shared.errors import ValidationError

        with pytest.raises(ValidationError):
            await to_buffer(123, "slack")

    async def test_raises_for_unsupported_type_dict(self):
        from chat_sdk.shared.errors import ValidationError

        with pytest.raises(ValidationError):
            await to_buffer({}, "slack")

    async def test_raises_for_unsupported_type_none(self):
        from chat_sdk.shared.errors import ValidationError

        with pytest.raises(ValidationError):
            await to_buffer(None, "slack")

    async def test_returns_none_when_throw_disabled(self):
        result = await to_buffer("string", "teams", throw_on_unsupported=False)
        assert result is None

    async def test_includes_platform_in_error(self):
        from chat_sdk.shared.errors import ValidationError

        try:
            await to_buffer("invalid", "slack")
        except ValidationError as e:
            assert e.adapter == "slack"


# ---------------------------------------------------------------------------
# to_buffer_sync
# ---------------------------------------------------------------------------


class TestToBufferSync:
    def test_bytes_passthrough(self):
        data = b"hello"
        result = to_buffer_sync(data, "slack")
        assert result == b"hello"

    def test_bytearray_conversion(self):
        data = bytearray(b"hello")
        result = to_buffer_sync(data, "slack")
        assert result == b"hello"

    def test_memoryview_conversion(self):
        data = memoryview(b"hello")
        result = to_buffer_sync(data, "slack")
        assert result == b"hello"

    def test_raises_for_unsupported_type(self):
        from chat_sdk.shared.errors import ValidationError

        with pytest.raises(ValidationError):
            to_buffer_sync("string", "slack")

    def test_returns_none_when_throw_disabled(self):
        result = to_buffer_sync("string", "teams", throw_on_unsupported=False)
        assert result is None

    def test_includes_platform_in_error(self):
        from chat_sdk.shared.errors import ValidationError

        try:
            to_buffer_sync("invalid", "teams")
        except ValidationError as e:
            assert e.adapter == "teams"


# ---------------------------------------------------------------------------
# buffer_to_data_uri
# ---------------------------------------------------------------------------


class TestBufferToDataUri:
    def test_default_mime_type(self):
        result = buffer_to_data_uri(b"hello")
        assert result == "data:application/octet-stream;base64,aGVsbG8="

    def test_custom_mime_type(self):
        result = buffer_to_data_uri(b"hello", "text/plain")
        assert result == "data:text/plain;base64,aGVsbG8="

    def test_image_mime_type(self):
        result = buffer_to_data_uri(bytes([0x89, 0x50, 0x4E, 0x47]), "image/png")
        assert result.startswith("data:image/png;base64,")

    def test_empty_buffer(self):
        result = buffer_to_data_uri(b"")
        assert result == "data:application/octet-stream;base64,"
