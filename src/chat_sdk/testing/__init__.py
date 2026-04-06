"""Test utilities for chat-sdk consumers.

Provides mock adapters and helpers for writing tests against the SDK.
Import from ``chat_sdk.testing`` instead of the main ``chat_sdk`` package.
"""

from chat_sdk.shared.mock_adapter import (
    MockAdapter,
    MockLogger,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
    mock_logger,
)

__all__ = [
    "MockAdapter",
    "MockLogger",
    "MockStateAdapter",
    "create_mock_adapter",
    "create_mock_state",
    "create_test_message",
    "mock_logger",
]
