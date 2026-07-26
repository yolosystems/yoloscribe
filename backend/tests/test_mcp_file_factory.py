"""Behavioral test for the factory-wired reactive handler set (YOL-501).

Exercises the full ① chain: a file object carrying the standard handler set
(``OnWriteEventHandler`` + ``NotificationBusHandler`` + ``KMSignalHandler`` — the
exact set ``mcp_file_factory`` attaches) fires all subscribers on a mutation.

The factory *functions* bind the config-owned global storage (``s3_storage`` →
``config``, which does import-time S3 I/O), so they can't be imported bare in a
unit env; here we wire the identical handler set onto file classes over a
``LocalStorageBackend`` and assert the side effects. The handler classes
themselves are additionally unit-tested in test_notifications / test_km_signal_handler.
"""
from __future__ import annotations

import km_signal_handler
from km_signal_handler import KMSignalHandler
from yoloscribe_io import (
    AgentDefinition,
    AgentMarkdownFile,
    NotificationBusHandler,
    OnWriteEventHandler,
    SkillMarkdownFile,
    WikiPageMarkdownFile,
)
from yoloscribe_io.storage import LocalStorageBackend


def _ctx(monkeypatch):
    store = LocalStorageBackend()
    enqueued: list[tuple] = []
    km_calls: list[tuple] = []
    monkeypatch.setattr(km_signal_handler, "dispatch", lambda *a: km_calls.append(a))
    bus = NotificationBusHandler("s", store, enqueue=lambda *a: enqueued.append(a))
    return store, enqueued, km_calls, bus


def _wire_page(store, bus):
    wiki = WikiPageMarkdownFile(site="s", page_path="projects/x", storage=store)
    wiki.add_handler(OnWriteEventHandler(storage=store, enqueue=lambda *a: None))
    wiki.add_handler(bus)
    wiki.add_handler(KMSignalHandler())
    return wiki


def test_agent_create_fires_bus_and_km(monkeypatch):
    store, enqueued, km_calls, bus = _ctx(monkeypatch)
    store.write(
        "s/.agents/watch/agent.md",
        "---\ntrigger: on_notify\nname: watch\nevents:\n  - agent_created\n---\n",
    )
    agent = AgentMarkdownFile(site="s", page_path="projects/x", agent_name="linear-sync", storage=store)
    agent.add_handler(bus)
    agent.add_handler(KMSignalHandler())

    agent.create(AgentDefinition(
        name="linear-sync", description="sync", trigger="on_write", type="page", skills=["linear"],
    ))

    raw = store.read("s/.user/notifications.md") or ""
    assert "agent_created" in raw          # notification-bus entry
    assert len(enqueued) == 1              # on_notify agent dispatched
    assert km_calls and km_calls[0][1] == "agent_provisioned"   # KM signal
    assert km_calls[0][2]["skills"] == ["linear"]
    assert km_calls[0][2]["page_type"] == "project"


def test_wiki_create_fires_km_page_structured_and_bus(monkeypatch):
    store, enqueued, km_calls, bus = _ctx(monkeypatch)
    _wire_page(store, bus).create("# X\n\n## Overview\n", user_id="u1")
    assert km_calls and km_calls[0][1] == "page_structured"
    assert km_calls[0][2]["sections"] == ["X", "Overview"]
    # page.created is structural → routed to the bus
    assert "page_created" in (store.read("s/.user/notifications.md") or "")


def test_wiki_write_fires_km_content_routed_but_not_bus(monkeypatch):
    store, enqueued, km_calls, bus = _ctx(monkeypatch)
    _wire_page(store, bus).write("# X\n", user_id="u1")
    assert km_calls and km_calls[0][1] == "content_routed"
    # page.written stays on the on_write fast-path — NOT routed to the bus
    assert (store.read("s/.user/notifications.md") or "") == ""


def test_agent_update_and_delete_reach_bus(monkeypatch):
    store, enqueued, km_calls, bus = _ctx(monkeypatch)

    def wire_agent():
        a = AgentMarkdownFile(site="s", page_path="projects/x", agent_name="a", storage=store)
        a.add_handler(bus)
        a.add_handler(KMSignalHandler())
        return a

    wire_agent().create(AgentDefinition(name="a", description="d", trigger="manual", type="page"))
    wire_agent().save(AgentDefinition(name="a", description="d2", trigger="manual", type="page"))
    wire_agent().delete()
    raw = store.read("s/.user/notifications.md") or ""
    assert "agent_created" in raw and "agent_updated" in raw and "agent_deleted" in raw
    # only agent.created carries a KM signal (agent_provisioned); update/delete don't
    assert [c[1] for c in km_calls] == ["agent_provisioned"]


def test_skill_create_update_delete_reach_bus_verbatim(monkeypatch):
    store, enqueued, km_calls, bus = _ctx(monkeypatch)
    custom = "---\nname: linear\ntools:\n  - linear_get_issue\nkeep: me\n---\n\n# Linear\n"

    def wire_skill():
        s = SkillMarkdownFile(site="s", skill_name="linear", storage=store)
        s.add_handler(bus)
        return s

    wire_skill().create_raw(custom)
    assert store.read("s/.skills/linear/SKILL.md") == custom   # verbatim, no re-serialize
    wire_skill().save_raw(custom + "\nmore\n")
    wire_skill().delete()
    raw = store.read("s/.user/notifications.md") or ""
    assert "skill_created" in raw and "skill_changed" in raw and "skill_deleted" in raw
    assert km_calls == []   # skills have no KM signal type
