"""Tests for the rolling per-channel request counter."""

import sys
import os
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from discord_bot import rate_tracker
from discord_bot.rate_tracker import HIGH_VOLUME_THRESHOLD


@pytest.fixture(autouse=True)
def clear_state():
    """Reset all counters before each test."""
    rate_tracker.reset()
    yield
    rate_tracker.reset()


class TestRecordRequest:
    def test_returns_false_below_threshold(self):
        for _ in range(HIGH_VOLUME_THRESHOLD):
            result = rate_tracker.record_request("ch1")
        assert result is False

    def test_returns_true_exactly_at_threshold_plus_one(self):
        for _ in range(HIGH_VOLUME_THRESHOLD):
            rate_tracker.record_request("ch1")
        result = rate_tracker.record_request("ch1")
        assert result is True

    def test_returns_false_after_threshold_crossed(self):
        for _ in range(HIGH_VOLUME_THRESHOLD + 1):
            rate_tracker.record_request("ch1")
        # Further requests should not fire again
        result = rate_tracker.record_request("ch1")
        assert result is False

    def test_different_channels_tracked_independently(self):
        for _ in range(HIGH_VOLUME_THRESHOLD + 1):
            rate_tracker.record_request("ch1")
        # ch2 is independent — should not be affected
        result = rate_tracker.record_request("ch2")
        assert result is False

    def test_channel_2_threshold_fires_independently(self):
        for _ in range(HIGH_VOLUME_THRESHOLD + 1):
            rate_tracker.record_request("ch1")
        for _ in range(HIGH_VOLUME_THRESHOLD):
            rate_tracker.record_request("ch2")
        result = rate_tracker.record_request("ch2")
        assert result is True

    def test_old_timestamps_pruned(self, monkeypatch):
        """Requests outside the window should not count toward the threshold."""
        # Simulate timestamps in the distant past by patching time.monotonic
        past = time.monotonic() - rate_tracker._WINDOW_SECONDS - 1

        # Pre-fill with stale timestamps by direct manipulation
        rate_tracker._timestamps["ch1"] = [past] * HIGH_VOLUME_THRESHOLD

        # A single fresh request should not cross the threshold
        result = rate_tracker.record_request("ch1")
        assert result is False

    def test_threshold_constant_is_50(self):
        assert HIGH_VOLUME_THRESHOLD == 50


class TestReset:
    def test_reset_specific_channel(self):
        for _ in range(HIGH_VOLUME_THRESHOLD + 1):
            rate_tracker.record_request("ch1")
        rate_tracker.reset("ch1")
        # After reset, ch1 counter starts fresh
        for _ in range(HIGH_VOLUME_THRESHOLD):
            rate_tracker.record_request("ch1")
        result = rate_tracker.record_request("ch1")
        assert result is True

    def test_reset_all(self):
        for ch in ["ch1", "ch2", "ch3"]:
            for _ in range(HIGH_VOLUME_THRESHOLD + 1):
                rate_tracker.record_request(ch)
        rate_tracker.reset()
        assert rate_tracker._timestamps == {}
