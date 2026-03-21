"""Unit tests for ChatRequest input validation (YOL-41, YOL-42, YOL-45)."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from pydantic import ValidationError

from models import ChatRequest, HistoryMessage
from config import MAX_CHAT_MESSAGE_BYTES, MAX_CHAT_CONTENT_BYTES, MAX_CHAT_HISTORY_TURNS


def _make_request(**kwargs):
    defaults = {"message": "hello", "current_content": "# Page"}
    return ChatRequest(**{**defaults, **kwargs})


# ---------------------------------------------------------------------------
# message max-length (YOL-45)
# ---------------------------------------------------------------------------


class TestMessageMaxLength:
    def test_message_at_limit_accepted(self):
        msg = "x" * MAX_CHAT_MESSAGE_BYTES
        req = _make_request(message=msg)
        assert len(req.message) == MAX_CHAT_MESSAGE_BYTES

    def test_message_over_limit_rejected(self):
        msg = "x" * (MAX_CHAT_MESSAGE_BYTES + 1)
        with pytest.raises(ValidationError) as exc_info:
            _make_request(message=msg)
        assert "message" in str(exc_info.value)

    def test_normal_message_accepted(self):
        req = _make_request(message="This is a normal message.")
        assert req.message == "This is a normal message."


# ---------------------------------------------------------------------------
# current_content truncation (YOL-42)
# ---------------------------------------------------------------------------


class TestCurrentContentTruncation:
    def test_content_at_limit_not_truncated(self):
        content = "x" * MAX_CHAT_CONTENT_BYTES
        req = _make_request(current_content=content)
        assert len(req.current_content) == MAX_CHAT_CONTENT_BYTES
        assert not req.current_content.endswith("[truncated]")

    def test_content_over_limit_truncated(self):
        content = "x" * (MAX_CHAT_CONTENT_BYTES + 500)
        req = _make_request(current_content=content)
        assert req.current_content.endswith("\n...[truncated]")
        assert len(req.current_content) == MAX_CHAT_CONTENT_BYTES + len("\n...[truncated]")

    def test_content_truncation_does_not_raise(self):
        # Should silently truncate, not reject.
        content = "y" * (MAX_CHAT_CONTENT_BYTES * 2)
        req = _make_request(current_content=content)
        assert "\n...[truncated]" in req.current_content

    def test_normal_content_unchanged(self):
        content = "# Hello\n\nSome content."
        req = _make_request(current_content=content)
        assert req.current_content == content


# ---------------------------------------------------------------------------
# history cap (YOL-41)
# ---------------------------------------------------------------------------


class TestHistoryCap:
    def _turns(self, n: int) -> list[dict]:
        return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(n)]

    def test_history_at_limit_unchanged(self):
        turns = self._turns(MAX_CHAT_HISTORY_TURNS)
        req = _make_request(history=turns)
        assert len(req.history) == MAX_CHAT_HISTORY_TURNS

    def test_history_over_limit_trimmed_to_most_recent(self):
        turns = self._turns(MAX_CHAT_HISTORY_TURNS + 5)
        req = _make_request(history=turns)
        assert len(req.history) == MAX_CHAT_HISTORY_TURNS
        # Most recent turns should be kept (last N).
        assert req.history[-1].content == f"turn {len(turns) - 1}"

    def test_oldest_turns_dropped(self):
        turns = self._turns(MAX_CHAT_HISTORY_TURNS + 3)
        req = _make_request(history=turns)
        # First turn in the trimmed list should be the (n+3 - cap)-th original turn.
        expected_first = turns[3]["content"]
        assert req.history[0].content == expected_first

    def test_empty_history_accepted(self):
        req = _make_request(history=[])
        assert req.history == []

    def test_short_history_unchanged(self):
        turns = self._turns(5)
        req = _make_request(history=turns)
        assert len(req.history) == 5
