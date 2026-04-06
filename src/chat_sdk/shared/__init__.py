"""Shared utilities for chat SDK adapters."""

from chat_sdk.shared.adapter_utils import extract_card, extract_files
from chat_sdk.shared.base_format_converter import BaseFormatConverter
from chat_sdk.shared.buffer_utils import (
    buffer_to_data_uri,
    to_buffer,
)
from chat_sdk.shared.card_utils import (
    BUTTON_STYLE_MAPPINGS,
    PlatformName,
    card_to_fallback_text,
    create_emoji_converter,
    escape_table_cell,
    map_button_style,
    render_gfm_table,
)
from chat_sdk.shared.errors import (
    AdapterError,
    AdapterRateLimitError,
    AuthenticationError,
    NetworkError,
    PermissionError,
    ResourceNotFoundError,
    ValidationError,
)
from chat_sdk.shared.markdown_parser import (
    ast_to_plain_text,
    parse_markdown,
    stringify_markdown,
    table_to_ascii,
    walk_ast,
)
from chat_sdk.shared.mock_adapter import (
    MockAdapter,
    MockLogger,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
    mock_logger,
)
from chat_sdk.shared.streaming_markdown import StreamingMarkdownRenderer

__all__ = [
    "AdapterError",
    "AdapterRateLimitError",
    "AuthenticationError",
    "BUTTON_STYLE_MAPPINGS",
    "BaseFormatConverter",
    "MockAdapter",
    "MockLogger",
    "MockStateAdapter",
    "NetworkError",
    "PermissionError",
    "PlatformName",
    "ResourceNotFoundError",
    "StreamingMarkdownRenderer",
    "ValidationError",
    "ast_to_plain_text",
    "buffer_to_data_uri",
    "card_to_fallback_text",
    "create_emoji_converter",
    "create_mock_adapter",
    "create_mock_state",
    "create_test_message",
    "escape_table_cell",
    "extract_card",
    "extract_files",
    "map_button_style",
    "mock_logger",
    "parse_markdown",
    "render_gfm_table",
    "stringify_markdown",
    "table_to_ascii",
    "to_buffer",
    "walk_ast",
]
