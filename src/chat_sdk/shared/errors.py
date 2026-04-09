"""Standardized error types for chat adapters."""

from __future__ import annotations


class AdapterError(Exception):
    """Base error class for adapter operations."""

    def __init__(self, message: str, adapter: str, code: str | None = None) -> None:
        super().__init__(message)
        self.adapter = adapter
        self.code = code


class AdapterRateLimitError(AdapterError):
    """Rate limit error."""

    def __init__(self, adapter: str, retry_after: float | None = None) -> None:
        msg = f"Rate limited by {adapter}"
        if retry_after is not None:
            msg += f", retry after {retry_after}s"
        super().__init__(msg, adapter, "RATE_LIMITED")
        self.retry_after = retry_after


class AuthenticationError(AdapterError):
    """Authentication error."""

    def __init__(self, adapter: str, message: str | None = None) -> None:
        super().__init__(
            message or f"Authentication failed for {adapter}",
            adapter,
            "AUTH_FAILED",
        )


class ResourceNotFoundError(AdapterError):
    """Resource not found error."""

    def __init__(self, adapter: str, resource_type: str, resource_id: str | None = None) -> None:
        id_part = f" '{resource_id}'" if resource_id else ""
        super().__init__(
            f"{resource_type}{id_part} not found in {adapter}",
            adapter,
            "NOT_FOUND",
        )
        self.resource_type = resource_type
        self.resource_id = resource_id


class AdapterPermissionError(AdapterError):
    """Permission denied error."""

    def __init__(self, adapter: str, action: str, required_scope: str | None = None) -> None:
        scope_part = f" (requires: {required_scope})" if required_scope else ""
        super().__init__(
            f"Permission denied: cannot {action} in {adapter}{scope_part}",
            adapter,
            "PERMISSION_DENIED",
        )
        self.action = action
        self.required_scope = required_scope


class ValidationError(AdapterError):
    """Validation error."""

    def __init__(self, adapter: str, message: str) -> None:
        super().__init__(message, adapter, "VALIDATION_ERROR")


class NetworkError(AdapterError):
    """Network connectivity error."""

    def __init__(self, adapter: str, message: str | None = None, original_error: Exception | None = None) -> None:
        super().__init__(
            message or f"Network error communicating with {adapter}",
            adapter,
            "NETWORK_ERROR",
        )
        self.original_error = original_error
