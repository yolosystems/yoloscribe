"""Tests for message parsing — [/page] prefix and response truncation."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from discord_bot.bot import parse_message, truncate_response


# ---------------------------------------------------------------------------
# parse_message
# ---------------------------------------------------------------------------


class TestParseMessage:
    def test_no_prefix_gives_root_page(self):
        file_path, text = parse_message("Hello world")
        assert file_path == "content.md"
        assert text == "Hello world"

    def test_page_prefix_parsed(self):
        file_path, text = parse_message("[/about] What is this page?")
        assert file_path == "about/content.md"
        assert text == "What is this page?"

    def test_nested_page_prefix(self):
        file_path, text = parse_message("[/docs/api] Show me the endpoints")
        assert file_path == "docs/api/content.md"
        assert text == "Show me the endpoints"

    def test_root_prefix_gives_root_page(self):
        """[/] with no page name → root page."""
        file_path, text = parse_message("[/] Hello")
        assert file_path == "content.md"
        assert text == "Hello"

    def test_prefix_whitespace_stripped(self):
        file_path, text = parse_message("[/about]    some message")
        assert text == "some message"

    def test_no_space_after_bracket(self):
        file_path, text = parse_message("[/about]message")
        assert file_path == "about/content.md"
        assert text == "message"

    def test_empty_message_after_prefix(self):
        file_path, text = parse_message("[/about] ")
        assert file_path == "about/content.md"
        assert text == ""

    def test_bracket_in_middle_not_parsed_as_prefix(self):
        """Prefix must be at the very start of the message."""
        file_path, text = parse_message("hello [/about] there")
        assert file_path == "content.md"
        assert text == "hello [/about] there"

    def test_leading_slash_stripped_from_file_path(self):
        """[/about] → about/content.md, not /about/content.md."""
        file_path, _ = parse_message("[/about] x")
        assert not file_path.startswith("/")


# ---------------------------------------------------------------------------
# truncate_response
# ---------------------------------------------------------------------------


class TestTruncateResponse:
    def test_short_response_unchanged(self):
        text = "Hello!"
        assert truncate_response(text) == text

    def test_exactly_2000_chars_unchanged(self):
        text = "x" * 2000
        assert truncate_response(text) == text

    def test_over_2000_chars_truncated(self):
        text = "x" * 2001
        result = truncate_response(text)
        assert len(result) <= 2000

    def test_truncated_response_has_suffix(self):
        text = "x" * 3000
        result = truncate_response(text)
        assert "truncated" in result

    def test_truncated_response_mentions_agentscribe(self):
        text = "y" * 3000
        result = truncate_response(text)
        assert "AgentScribe" in result

    def test_truncated_response_starts_with_original(self):
        text = "hello " + "x" * 3000
        result = truncate_response(text)
        assert result.startswith("hello")
