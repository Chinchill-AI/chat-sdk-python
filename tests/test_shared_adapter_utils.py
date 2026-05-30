"""Tests for shared adapter utility functions.

Port of packages/adapter-shared/src/adapter-utils.test.ts -- specifically the
``extractPostableAttachments`` cases added in vercel/chat#485.
"""

from __future__ import annotations

from chat_sdk.cards import Card
from chat_sdk.shared.adapter_utils import extract_postable_attachments
from chat_sdk.types import Attachment, PostableAst, PostableMarkdown, PostableRaw


class TestExtractPostableAttachmentsPresent:
    """Attachments present on the message are returned verbatim."""

    def test_extracts_from_postable_raw(self):
        attachments = [
            Attachment(type="file", data=b"content1", name="file1.txt"),
            Attachment(type="file", data=b"content2", name="file2.txt"),
        ]
        message = PostableRaw(raw="Text", attachments=attachments)
        result = extract_postable_attachments(message)
        assert result is attachments
        assert len(result) == 2

    def test_extracts_from_postable_markdown(self):
        attachments = [
            Attachment(type="image", data=b"image", name="image.png", mime_type="image/png"),
        ]
        message = PostableMarkdown(markdown="**Text**", attachments=attachments)
        result = extract_postable_attachments(message)
        assert result == attachments
        assert result[0].mime_type == "image/png"

    def test_extracts_from_postable_ast(self):
        attachments = [Attachment(type="file", data=b"doc", name="doc.pdf")]
        message = PostableAst(ast={"type": "root", "children": []}, attachments=attachments)
        result = extract_postable_attachments(message)
        assert result is attachments

    def test_extracts_from_dict_message(self):
        attachments = [Attachment(type="image", data=b"x")]
        result = extract_postable_attachments({"raw": "Text", "attachments": attachments})
        assert result == attachments


class TestExtractPostableAttachmentsEmptyOrMissing:
    """Empty or missing attachments yield an empty list."""

    def test_empty_attachments_list(self):
        assert extract_postable_attachments(PostableRaw(raw="Text", attachments=[])) == []

    def test_none_attachments(self):
        assert extract_postable_attachments(PostableRaw(raw="Text", attachments=None)) == []

    def test_postable_raw_without_attachments(self):
        assert extract_postable_attachments(PostableRaw(raw="Just text")) == []

    def test_postable_markdown_without_attachments(self):
        assert extract_postable_attachments(PostableMarkdown(markdown="**Bold**")) == []

    def test_dict_without_attachments(self):
        assert extract_postable_attachments({"raw": "Just text"}) == []


class TestExtractPostableAttachmentsNonObject:
    """Non-message inputs yield an empty list rather than raising."""

    def test_plain_string(self):
        assert extract_postable_attachments("Hello world") == []

    def test_card_element(self):
        assert extract_postable_attachments(Card(title="Test")) == []
