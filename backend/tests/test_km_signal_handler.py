"""Tests for KMSignalHandler (YOL-500) — the event-bus subscriber that emits
typed KM signals for mutation events, replacing YOL-490's explicit calls."""
from __future__ import annotations

import km_signal_handler
from km_signal_handler import KMSignalHandler
from yoloscribe_io import Event, EventType


def _dispatched(event, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(km_signal_handler, "dispatch", lambda *a: calls.append(a))
    KMSignalHandler().handle(event)
    return calls


class TestKMSignalHandler:
    def test_page_created_emits_page_structured(self, monkeypatch):
        calls = _dispatched(
            Event(EventType.PAGE_CREATED, {
                "site": "s", "page_path": "projects/x", "content": "# X\n\n## Overview\n\n## Tasks\n",
            }),
            monkeypatch,
        )
        assert len(calls) == 1
        site, signal_type, params = calls[0]
        assert site == "s"
        assert signal_type == "page_structured"
        assert params["page_type"] == "project"
        assert params["sections"] == ["X", "Overview", "Tasks"]
        assert params["target"] == {"system": "yoloscribe", "path": "projects/x"}

    def test_page_written_emits_content_routed(self, monkeypatch):
        calls = _dispatched(
            Event(EventType.PAGE_WRITTEN, {"site": "s", "page_path": "accounts/acme"}),
            monkeypatch,
        )
        assert calls[0][1] == "content_routed"
        assert calls[0][2]["integration"] == "replace"
        assert calls[0][2]["page_type"] == "account"

    def test_agent_created_emits_agent_provisioned(self, monkeypatch):
        calls = _dispatched(
            Event(EventType.AGENT_CREATED, {
                "site": "s", "page_path": "projects/x", "agent_type": "page",
                "skills": ["linear"], "trigger": "on_write",
            }),
            monkeypatch,
        )
        assert calls[0][1] == "agent_provisioned"
        assert calls[0][2] == {
            "page_type": "project",
            "agent_type": "page",
            "skills": ["linear"],
            "trigger": "on_write",
            "host": {"path": "projects/x"},
        }

    def test_missing_site_no_dispatch(self, monkeypatch):
        calls = _dispatched(
            Event(EventType.PAGE_CREATED, {"page_path": "x", "content": ""}),
            monkeypatch,
        )
        assert calls == []

    def test_unmapped_event_no_dispatch(self, monkeypatch):
        # page.deleted / agent.updated have no mutation-shaped KM signal type.
        calls = _dispatched(
            Event(EventType.PAGE_DELETED, {"site": "s", "page_path": "x"}),
            monkeypatch,
        )
        assert calls == []
