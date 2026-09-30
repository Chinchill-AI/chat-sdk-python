"""Shared utilities for chat SDK adapters."""

from chat_sdk.shared.adapter_utils import (
    extract_card,
    extract_files,
    extract_postable_attachments,
)
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
from chat_sdk.shared.code_fences import normalize_code_fences
from chat_sdk.shared.download import (
    AttachmentResponse,
    AttachmentTransport,
    download_attachment,
    validate_attachment_url,
)
from chat_sdk.shared.errors import (
    AdapterError,
    AdapterPermissionError,
    AdapterRateLimitError,
    AuthenticationError,
    NetworkError,
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
from chat_sdk.shared.mentions import (
    MentionReplacer,
    mask_code_spans,
    replace_bare_mentions,
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
    "AttachmentResponse",
    "AttachmentTransport",
    "AuthenticationError",
    "BUTTON_STYLE_MAPPINGS",
    "BaseFormatConverter",
    "MentionReplacer",
    "MockAdapter",
    "MockLogger",
    "MockStateAdapter",
    "NetworkError",
    "AdapterPermissionError",
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
    "download_attachment",
    "escape_table_cell",
    "extract_card",
    "extract_files",
    "extract_postable_attachments",
    "map_button_style",
    "mask_code_spans",
    "mock_logger",
    "normalize_code_fences",
    "parse_markdown",
    "render_gfm_table",
    "replace_bare_mentions",
    "stringify_markdown",
    "table_to_ascii",
    "to_buffer",
    "validate_attachment_url",
    "walk_ast",
]
