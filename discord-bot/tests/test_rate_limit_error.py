"""Tests for RateLimitError and its retry_after attribute."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from discord_bot.bot import RateLimitError


class TestRateLimitError:
    def test_retry_after_stored(self):
        exc = RateLimitError("42")
        assert exc.retry_after == "42"

    def test_is_exception(self):
        assert isinstance(RateLimitError("5"), Exception)

    def test_message_contains_retry_after(self):
        exc = RateLimitError("30")
        assert "30" in str(exc)

    def test_unknown_retry_after(self):
        exc = RateLimitError("unknown")
        assert exc.retry_after == "unknown"
