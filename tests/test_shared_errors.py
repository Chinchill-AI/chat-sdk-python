"""Tests for chat_sdk.shared.errors module."""

from __future__ import annotations

from chat_sdk.shared.errors import (
    AdapterError,
    AdapterPermissionError,
    AdapterRateLimitError,
    AuthenticationError,
    NetworkError,
    ResourceNotFoundError,
    ValidationError,
)


class TestAdapterError:
    """Tests for base AdapterError."""

    def test_creation(self):
        err = AdapterError("something broke", adapter="slack")
        assert str(err) == "something broke"
        assert err.adapter == "slack"
        assert err.code is None

    def test_with_code(self):
        err = AdapterError("bad request", adapter="github", code="BAD_REQUEST")
        assert err.code == "BAD_REQUEST"

    def test_is_exception(self):
        err = AdapterError("test", adapter="test")
        assert isinstance(err, Exception)

    def test_can_be_raised_and_caught(self):
        try:
            raise AdapterError("oops", adapter="slack")
        except AdapterError as e:
            assert e.adapter == "slack"


class TestAdapterRateLimitError:
    """Tests for AdapterRateLimitError."""

    def test_without_retry_after(self):
        err = AdapterRateLimitError(adapter="slack")
        assert "Rate limited by slack" in str(err)
        assert err.retry_after is None
        assert err.code == "RATE_LIMITED"
        assert err.adapter == "slack"

    def test_with_retry_after(self):
        err = AdapterRateLimitError(adapter="telegram", retry_after=30.0)
        assert "retry after 30.0s" in str(err)
        assert err.retry_after == 30.0

    def test_is_adapter_error(self):
        err = AdapterRateLimitError(adapter="whatsapp")
        assert isinstance(err, AdapterError)
        assert isinstance(err, Exception)


class TestAuthenticationError:
    """Tests for AuthenticationError."""

    def test_default_message(self):
        err = AuthenticationError(adapter="github")
        assert "Authentication failed for github" in str(err)
        assert err.code == "AUTH_FAILED"
        assert err.adapter == "github"

    def test_custom_message(self):
        err = AuthenticationError(adapter="slack", message="Token expired")
        assert "Token expired" in str(err)
        assert err.code == "AUTH_FAILED"

    def test_is_adapter_error(self):
        err = AuthenticationError(adapter="gchat")
        assert isinstance(err, AdapterError)


class TestResourceNotFoundError:
    """Tests for ResourceNotFoundError."""

    def test_without_resource_id(self):
        err = ResourceNotFoundError(adapter="github", resource_type="channel")
        assert "channel" in str(err)
        assert "not found in github" in str(err)
        assert err.code == "NOT_FOUND"
        assert err.resource_type == "channel"
        assert err.resource_id is None

    def test_with_resource_id(self):
        err = ResourceNotFoundError(adapter="slack", resource_type="message", resource_id="msg-123")
        assert "message" in str(err)
        assert "'msg-123'" in str(err)
        assert err.resource_id == "msg-123"

    def test_is_adapter_error(self):
        err = ResourceNotFoundError(adapter="test", resource_type="thread")
        assert isinstance(err, AdapterError)


class TestPermissionError:
    """Tests for PermissionError (AdapterPermissionError)."""

    def test_without_scope(self):
        err = AdapterPermissionError(adapter="slack", action="post_message")
        assert "Permission denied" in str(err)
        assert "post_message" in str(err)
        assert "slack" in str(err)
        assert err.code == "PERMISSION_DENIED"
        assert err.action == "post_message"
        assert err.required_scope is None

    def test_with_scope(self):
        err = AdapterPermissionError(
            adapter="slack",
            action="delete_message",
            required_scope="chat:write",
        )
        assert "requires: chat:write" in str(err)
        assert err.required_scope == "chat:write"

    def test_is_adapter_error(self):
        err = AdapterPermissionError(adapter="test", action="test")
        assert isinstance(err, AdapterError)


class TestValidationError:
    """Tests for ValidationError."""

    def test_creation(self):
        err = ValidationError(adapter="whatsapp", message="Invalid thread ID format")
        assert "Invalid thread ID format" in str(err)
        assert err.adapter == "whatsapp"
        assert err.code == "VALIDATION_ERROR"

    def test_is_adapter_error(self):
        err = ValidationError(adapter="test", message="test")
        assert isinstance(err, AdapterError)


class TestNetworkError:
    """Tests for NetworkError."""

    def test_default_message(self):
        err = NetworkError(adapter="telegram")
        assert "Network error communicating with telegram" in str(err)
        assert err.code == "NETWORK_ERROR"
        assert err.original_error is None

    def test_custom_message(self):
        err = NetworkError(adapter="slack", message="Connection timeout")
        assert "Connection timeout" in str(err)

    def test_with_original_error(self):
        original = ConnectionError("refused")
        err = NetworkError(adapter="github", original_error=original)
        assert err.original_error is original

    def test_is_adapter_error(self):
        err = NetworkError(adapter="test")
        assert isinstance(err, AdapterError)
