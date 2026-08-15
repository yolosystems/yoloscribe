"""The write reason: capture, validation, and propagation (YOL-527).

YoloScribe only ever sees a committed tool call, never the conversation that
produced it. `reason` is the caller's distillate of that conversation, and the
whole point is that it survives all the way to the learning rail — a reason that
is accepted by the tool and then dropped on the floor is worse than none, because
it looks like the loop is closed.

So these tests come in three layers:

1. `_require_reason` rejects the degenerate values a required parameter still
   admits ("" / "   " / "x").
2. `WikiPageMarkdownFile` puts the reason on the mutation event, and leaves it
   off entirely when absent.
3. `KMSignalHandler` carries it into the emitted KM signal params.

What is deliberately NOT asserted here: that YoloBrain stores it. Its
`ContentRoutedParams` / `PageStructuredParams` models declare no `reason` field
and pydantic defaults to `extra="ignore"`, so today the value is dropped on
arrival. That is tracked as YOL-550 and is a change in a different repo; pinning
it from here would be testing someone else's schema.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import km_signal_handler
from km_signal_handler import KMSignalHandler
from yoloscribe_io import WikiPageMarkdownFile
from yoloscribe_io.storage import LocalStorageBackend


class _Recorder:
    """Captures the event payloads a file object emits."""

    def __init__(self) -> None:
        self.events: list = []

    def handle(self, event) -> None:
        self.events.append(event)


def _page(store, page_path="projects/x"):
    return WikiPageMarkdownFile(site="s", page_path=page_path, storage=store)


# ── Layer 1: validation ───────────────────────────────────────────────────────


class TestRequireReason:
    @pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
    def test_empty_is_rejected(self, bad):
        """A required parameter still admits the empty string."""
        from mcp_server import _require_reason

        with pytest.raises(ValueError, match="reason is required"):
            _require_reason(bad)

    @pytest.mark.parametrize("bad", ["x", ".", "wip", "update"])
    def test_placeholder_is_rejected(self, bad):
        from mcp_server import _require_reason

        with pytest.raises(ValueError, match="too short"):
            _require_reason(bad)

    @pytest.mark.parametrize("good", [
        "user asked to drop the deprecated Okta steps",
        "superseded by projects/x/design",
        "filing Q3 notes under planning/",
    ])
    def test_real_reasons_pass(self, good):
        from mcp_server import _require_reason

        _require_reason(good)  # must not raise

    def test_the_floor_does_not_reject_terse_but_real(self):
        """The validator screens placeholders, not brevity.

        Judging whether prose is *meaningful* is not something a length check can
        do, and a floor set high enough to try would start rejecting honest
        one-word-per-concept reasons. The pressure lives in the parameter
        description instead.
        """
        from mcp_server import _require_reason

        _require_reason("typo fix")


# ── Layer 2: the reason reaches the mutation event ────────────────────────────


class TestEventPayload:
    def test_write_puts_reason_on_the_event(self):
        store, rec = LocalStorageBackend(), _Recorder()
        page = _page(store)
        page.add_handler(rec)

        page.write("# X\n", user_id="u1", reason="dropping the Okta section")

        assert rec.events[0].payload["reason"] == "dropping the Okta section"

    def test_create_puts_reason_on_the_event(self):
        store, rec = LocalStorageBackend(), _Recorder()
        page = _page(store)
        page.add_handler(rec)

        page.create("# X\n", user_id="u1", reason="splitting auth out of onboarding")

        assert rec.events[0].payload["reason"] == "splitting auth out of onboarding"

    def test_conditional_write_puts_reason_on_the_event(self):
        """The etag path is a separate emit and has been forgotten before."""
        store, rec = LocalStorageBackend(), _Recorder()
        page = _page(store)
        page.write("# v1\n")
        _, etag = store.read_with_etag(page.key)
        assert etag, "need a real etag or this exercises the unconditional path instead"
        page.add_handler(rec)

        assert page.write_conditional("# v2\n", etag, user_id="u1", reason="applying review notes")
        assert rec.events[0].payload["reason"] == "applying review notes"

    def test_absent_reason_is_omitted_not_blank(self):
        """An empty `reason:` line would be noise in the owner's inbox.

        NotificationBusHandler renders every non-internal payload key into the
        notification entry, so the key must be absent rather than empty.
        """
        store, rec = LocalStorageBackend(), _Recorder()
        page = _page(store)
        page.add_handler(rec)

        page.write("# X\n", user_id="u1")

        assert "reason" not in rec.events[0].payload


# ── Layer 3: the reason reaches the KM signal ─────────────────────────────────


class TestKMSignalPropagation:
    def _km_calls(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(km_signal_handler, "dispatch", lambda *a: calls.append(a))
        return calls

    def test_content_routed_carries_reason(self, monkeypatch):
        calls = self._km_calls(monkeypatch)
        store = LocalStorageBackend()
        page = _page(store)
        page.add_handler(KMSignalHandler())

        page.write("# X\n", user_id="u1", reason="user asked to trim the intro")

        assert calls[0][1] == "content_routed"
        assert calls[0][2]["reason"] == "user asked to trim the intro"

    def test_page_structured_carries_reason(self, monkeypatch):
        calls = self._km_calls(monkeypatch)
        store = LocalStorageBackend()
        page = _page(store)
        page.add_handler(KMSignalHandler())

        page.create("# X\n\n## Overview\n", user_id="u1", reason="new page for the Q3 plan")

        assert calls[0][1] == "page_structured"
        assert calls[0][2]["reason"] == "new page for the Q3 plan"
        # The pre-existing params must survive the addition.
        assert calls[0][2]["sections"] == ["X", "Overview"]

    def test_params_are_unchanged_when_no_reason_given(self, monkeypatch):
        """Callers that supply nothing must emit exactly the pre-YOL-527 shape.

        An always-present `reason: ""` would change the params for every
        first-party write path, none of which were touched by this change.
        """
        calls = self._km_calls(monkeypatch)
        store = LocalStorageBackend()
        page = _page(store)
        page.add_handler(KMSignalHandler())

        page.write("# X\n", user_id="u1")

        assert "reason" not in calls[0][2]
