import re
from unittest.mock import MagicMock, call

import pytest

from yoloscribe_io.storage import LocalStorageBackend
from yoloscribe_io.notifications import (
    NO_DISPATCH_EVENTS,
    NotificationsMarkdownFile,
    _format_entry,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _agent_store(*agents: tuple[str, str]) -> LocalStorageBackend:
    return LocalStorageBackend({key: content for key, content in agents})


_ON_NOTIFY_AGENT = "---\ntrigger: on_notify\nname: notifier\n---\nHandles notifications."
_ON_WRITE_AGENT = "---\ntrigger: on_write\nname: writer\n---\nHandles page writes."
_MANUAL_AGENT = "---\ntrigger: manual\nname: manual\n---\nManual only."


# ── _format_entry ─────────────────────────────────────────────────────────────

def test_format_entry_contains_event_type():
    entry = _format_entry("agent_success", {"page": "s/p.md"})
    assert "agent_success" in entry


def test_format_entry_contains_payload_keys_and_values():
    entry = _format_entry("page_shared", {"requester": "a@b.com", "page": "blog"})
    assert "requester: a@b.com" in entry
    assert "page: blog" in entry


def test_format_entry_timestamp_format():
    entry = _format_entry("x", {})
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", entry)


def test_format_entry_heading_format():
    entry = _format_entry("my_event", {})
    assert entry.startswith("## ")
    assert "— my_event" in entry


def test_format_entry_ends_with_newline():
    entry = _format_entry("x", {"k": "v"})
    assert entry.endswith("\n")


def test_format_entry_empty_payload():
    entry = _format_entry("x", {})
    assert "## " in entry


# ── NO_DISPATCH_EVENTS ────────────────────────────────────────────────────────

def test_no_dispatch_includes_agent_success():
    assert "agent_success" in NO_DISPATCH_EVENTS


def test_no_dispatch_includes_agent_failure():
    assert "agent_failure" in NO_DISPATCH_EVENTS


# ── NotificationsMarkdownFile — construction ──────────────────────────────────

def test_key_is_user_notifications():
    f = NotificationsMarkdownFile("mysite", LocalStorageBackend())
    assert f.key == "mysite/.user/notifications.md"
    assert f.path == ".user/notifications.md"


# ── notify — appending ────────────────────────────────────────────────────────

@pytest.fixture
def store():
    return LocalStorageBackend()


def test_notify_writes_entry_to_storage(store):
    f = NotificationsMarkdownFile("s", store)
    f.notify("page_shared", {"requester": "a@b.com"})
    raw = store.read("s/.user/notifications.md")
    assert raw is not None
    assert "page_shared" in raw
    assert "requester: a@b.com" in raw


def test_notify_appends_to_existing_content(store):
    store.write("s/.user/notifications.md", "## existing entry\n\n")
    f = NotificationsMarkdownFile("s", store)
    f.notify("new_event", {"k": "v"})
    raw = store.read("s/.user/notifications.md")
    assert "existing entry" in raw
    assert "new_event" in raw


def test_notify_multiple_calls_all_appear(store):
    f = NotificationsMarkdownFile("s", store)
    f.notify("event_one", {"a": "1"})
    f.notify("event_two", {"b": "2"})
    raw = store.read("s/.user/notifications.md")
    assert "event_one" in raw
    assert "event_two" in raw


def test_notify_updates_raw_content_cache(store):
    f = NotificationsMarkdownFile("s", store)
    f.notify("x", {})
    assert f.raw_content == store.read("s/.user/notifications.md")


def test_notify_always_rereads_storage(store):
    f = NotificationsMarkdownFile("s", store)
    f.notify("first", {})
    # Simulate an external write
    store.write("s/.user/notifications.md", store.read("s/.user/notifications.md") + "## external\n\n")
    f.notify("second", {})
    raw = store.read("s/.user/notifications.md")
    assert "first" in raw
    assert "external" in raw
    assert "second" in raw


# ── notify — dispatch ─────────────────────────────────────────────────────────

def test_notify_dispatches_to_on_notify_agent(store):
    store.write("s/.agents/notifier/agent.md", _ON_NOTIFY_AGENT)
    enqueue = MagicMock()
    f = NotificationsMarkdownFile("s", store, enqueue=enqueue)
    f.notify("page_shared", {"page": "blog"})
    enqueue.assert_called_once()
    agent_key, notif_key, prompt, user_id = enqueue.call_args[0]
    assert agent_key == "s/.agents/notifier/agent.md"
    assert notif_key == "s/.user/notifications.md"
    assert "page_shared" in prompt
    assert user_id == ""


def test_notify_passes_user_id_to_enqueue(store):
    store.write("s/.agents/n/agent.md", _ON_NOTIFY_AGENT)
    enqueue = MagicMock()
    f = NotificationsMarkdownFile("s", store, enqueue=enqueue)
    f.notify("x", {}, user_id="user-42")
    _, _, _, user_id = enqueue.call_args[0]
    assert user_id == "user-42"


def test_notify_skips_on_write_agent(store):
    store.write("s/.agents/writer/agent.md", _ON_WRITE_AGENT)
    enqueue = MagicMock()
    f = NotificationsMarkdownFile("s", store, enqueue=enqueue)
    f.notify("x", {})
    enqueue.assert_not_called()


def test_notify_skips_manual_agent(store):
    store.write("s/.agents/manual/agent.md", _MANUAL_AGENT)
    enqueue = MagicMock()
    f = NotificationsMarkdownFile("s", store, enqueue=enqueue)
    f.notify("x", {})
    enqueue.assert_not_called()


def test_notify_dispatches_multiple_on_notify_agents(store):
    store.write("s/.agents/a/agent.md", _ON_NOTIFY_AGENT)
    store.write("s/.agents/b/agent.md", _ON_NOTIFY_AGENT)
    store.write("s/.agents/c/agent.md", _ON_WRITE_AGENT)
    enqueue = MagicMock()
    f = NotificationsMarkdownFile("s", store, enqueue=enqueue)
    f.notify("x", {})
    assert enqueue.call_count == 2


def test_notify_skips_non_agent_md_files(store):
    store.write("s/.agents/n/other.txt", "trigger: on_notify")
    enqueue = MagicMock()
    f = NotificationsMarkdownFile("s", store, enqueue=enqueue)
    f.notify("x", {})
    enqueue.assert_not_called()


def test_notify_prompt_contains_entry(store):
    store.write("s/.agents/n/agent.md", _ON_NOTIFY_AGENT)
    enqueue = MagicMock()
    f = NotificationsMarkdownFile("s", store, enqueue=enqueue)
    f.notify("confirm_page_change", {"page": "blog/posts"})
    _, _, prompt, _ = enqueue.call_args[0]
    assert "confirm_page_change" in prompt
    assert "blog/posts" in prompt


# ── NO_DISPATCH_EVENTS — loop guard ──────────────────────────────────────────

def test_agent_success_not_dispatched(store):
    store.write("s/.agents/n/agent.md", _ON_NOTIFY_AGENT)
    enqueue = MagicMock()
    f = NotificationsMarkdownFile("s", store, enqueue=enqueue)
    f.notify("agent_success", {"agent": "my-agent"})
    enqueue.assert_not_called()


def test_agent_failure_not_dispatched(store):
    store.write("s/.agents/n/agent.md", _ON_NOTIFY_AGENT)
    enqueue = MagicMock()
    f = NotificationsMarkdownFile("s", store, enqueue=enqueue)
    f.notify("agent_failure", {"reason": "timeout"})
    enqueue.assert_not_called()


def test_agent_success_still_appended_to_storage(store):
    f = NotificationsMarkdownFile("s", store)
    f.notify("agent_success", {"agent": "x"})
    raw = store.read("s/.user/notifications.md")
    assert "agent_success" in raw


# ── enqueue=None (no dispatch) ────────────────────────────────────────────────

def test_no_enqueue_still_writes_notification(store):
    f = NotificationsMarkdownFile("s", store, enqueue=None)
    f.notify("page_shared", {"page": "blog"})
    assert "page_shared" in store.read("s/.user/notifications.md")


def test_no_enqueue_does_not_raise(store):
    store.write("s/.agents/n/agent.md", _ON_NOTIFY_AGENT)
    f = NotificationsMarkdownFile("s", store, enqueue=None)
    f.notify("page_shared", {})  # must not raise
