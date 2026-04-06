"""Error types for chat-sdk."""

from __future__ import annotations


class ChatError(Exception):
    """Base error for chat SDK operations."""

    pass


class LockError(ChatError):
    """Raised when a thread lock cannot be acquired."""

    def __init__(self, thread_id: str, message: str | None = None) -> None:
        self.thread_id = thread_id
        super().__init__(message or f"Could not acquire lock for thread {thread_id}")


class ChatNotImplementedError(ChatError):
    """Raised when an adapter method is not implemented."""

    def __init__(self, adapter: str, method: str) -> None:
        self.adapter = adapter
        self.method = method
        super().__init__(f"{adapter} does not support {method}")


class RateLimitError(ChatError):
    """Raised when a platform rate limit is hit."""

    def __init__(self, adapter: str, retry_after: float | None = None) -> None:
        self.adapter = adapter
        self.retry_after = retry_after
        msg = f"Rate limited by {adapter}"
        if retry_after is not None:
            msg += f", retry after {retry_after}s"
        super().__init__(msg)
