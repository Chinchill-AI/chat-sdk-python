"""Port of adapter-github/src/cards.test.ts -- GitHub card rendering tests.

Tests card_to_github_markdown and card_to_plain_text.
"""

from __future__ import annotations

from chat_sdk.adapters.github.cards import card_to_github_markdown, card_to_plain_text
from chat_sdk.cards import Card, CardLink

# ---------------------------------------------------------------------------
# cardToGitHubMarkdown
# ---------------------------------------------------------------------------


class TestCardToGitHubMarkdown:
    """Tests for card_to_github_markdown."""

    def test_simple_title(self):
        card = {"type": "card", "title": "Hello World", "children": []}
        result = card_to_github_markdown(card)
        assert "**Hello World**" in result

    def test_title_and_subtitle(self):
        card = {
            "type": "card",
            "title": "Order #1234",
            "subtitle": "Status update",
            "children": [],
        }
        result = card_to_github_markdown(card)
        assert "Order" in result
        assert "Status update" in result

    def test_text_content(self):
        card = {
            "type": "card",
            "title": "Notification",
            "children": [{"type": "text", "content": "Your order has been shipped!"}],
        }
        result = card_to_github_markdown(card)
        assert "Your order has been shipped!" in result

    def test_fields(self):
        card = {
            "type": "card",
            "title": "Order Details",
            "children": [
                {
                    "type": "fields",
                    "children": [
                        {"type": "field", "label": "Order ID", "value": "12345"},
                        {"type": "field", "label": "Status", "value": "Shipped"},
                    ],
                }
            ],
        }
        result = card_to_github_markdown(card)
        assert "Order ID" in result
        assert "12345" in result
        assert "Status" in result
        assert "Shipped" in result

    def test_link_buttons(self):
        card = {
            "type": "card",
            "title": "Actions",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {
                            "type": "link-button",
                            "url": "https://example.com/track",
                            "label": "Track Order",
                        },
                        {
                            "type": "link-button",
                            "url": "https://example.com/help",
                            "label": "Get Help",
                        },
                    ],
                }
            ],
        }
        result = card_to_github_markdown(card)
        assert "[Track Order](https://example.com/track)" in result
        assert "[Get Help](https://example.com/help)" in result

    def test_action_buttons_as_bold_text(self):
        card = {
            "type": "card",
            "title": "Approve?",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {
                            "type": "button",
                            "id": "approve",
                            "label": "Approve",
                            "style": "primary",
                        },
                        {
                            "type": "button",
                            "id": "reject",
                            "label": "Reject",
                            "style": "danger",
                        },
                    ],
                }
            ],
        }
        result = card_to_github_markdown(card)
        assert "**[Approve]**" in result
        assert "**[Reject]**" in result

    def test_image(self):
        card = {
            "type": "card",
            "title": "Image Card",
            "children": [
                {
                    "type": "image",
                    "url": "https://example.com/image.png",
                    "alt": "Example image",
                }
            ],
        }
        result = card_to_github_markdown(card)
        assert "![Example image](https://example.com/image.png)" in result

    def test_divider(self):
        card = {
            "type": "card",
            "children": [
                {"type": "text", "content": "Before"},
                {"type": "divider"},
                {"type": "text", "content": "After"},
            ],
        }
        result = card_to_github_markdown(card)
        assert "---" in result

    def test_section(self):
        card = {
            "type": "card",
            "children": [
                {
                    "type": "section",
                    "children": [{"type": "text", "content": "Section content"}],
                }
            ],
        }
        result = card_to_github_markdown(card)
        assert "Section content" in result

    def test_text_styles(self):
        card = {
            "type": "card",
            "children": [
                {"type": "text", "content": "Normal text"},
                {"type": "text", "content": "Bold text", "style": "bold"},
                {"type": "text", "content": "Muted text", "style": "muted"},
            ],
        }
        result = card_to_github_markdown(card)
        assert "Normal text" in result
        assert "**Bold text**" in result
        assert "_Muted text_" in result


# ---------------------------------------------------------------------------
# cardToPlainText
# ---------------------------------------------------------------------------


class TestGitHubCardToPlainText:
    """Tests for card_to_plain_text."""

    def test_plain_text_from_card(self):
        card = {
            "type": "card",
            "title": "Hello",
            "subtitle": "World",
            "children": [
                {"type": "text", "content": "Some content"},
                {
                    "type": "fields",
                    "children": [{"type": "field", "label": "Key", "value": "Value"}],
                },
            ],
        }
        result = card_to_plain_text(card)
        assert "Hello" in result
        assert "World" in result
        assert "Some content" in result
        assert "Key: Value" in result


# ---------------------------------------------------------------------------
# cardToGitHubMarkdown with CardLink helper
# ---------------------------------------------------------------------------


class TestCardToGitHubMarkdownWithCardLink:
    """Tests for card rendering with CardLink elements."""

    def test_card_link_renders_as_markdown_link(self):
        card = Card(children=[CardLink(url="https://example.com", label="Click here")])
        markdown = card_to_github_markdown(card)
        assert "[Click here](https://example.com)" in markdown
