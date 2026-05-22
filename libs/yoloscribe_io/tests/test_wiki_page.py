import json
from unittest.mock import MagicMock

import pytest

from yoloscribe_io.events import EventEmitter, EventType
from yoloscribe_io.storage import LocalStorageBackend
from yoloscribe_io.wiki_page import (
    OnWriteEventHandler,
    PageSettings,
    SettingsData,
    SharedUser,
    WikiPageMarkdownFile,
)


# ── helpers ───────────────────────────────────────────────────────────────────

class CapturingHandler:
    def __init__(self):
        self.events = []

    def handle(self, event):
        self.events.append(event)


# ── SharedUser ────────────────────────────────────────────────────────────────

def test_shared_user_to_dict():
    u = SharedUser(email="a@b.com", access="view")
    assert u.to_dict() == {"email": "a@b.com", "access": "view"}


# ── SettingsData ──────────────────────────────────────────────────────────────

def test_settings_data_default():
    s = SettingsData.default()
    assert s.visibility == "private"
    assert s.shared_with == []


def test_settings_data_to_dict():
    s = SettingsData(visibility="shared", shared_with=[SharedUser("x@y.com", "write")])
    d = s.to_dict()
    assert d["visibility"] == "shared"
    assert d["shared_with"] == [{"email": "x@y.com", "access": "write"}]


def test_settings_data_from_dict_roundtrip():
    original = SettingsData(
        visibility="public",
        shared_with=[SharedUser("a@b.com", "view"), SharedUser("c@d.com", "write")],
    )
    restored = SettingsData.from_dict(original.to_dict())
    assert restored.visibility == "public"
    assert len(restored.shared_with) == 2
    assert restored.shared_with[0].email == "a@b.com"
    assert restored.shared_with[1].access == "write"


def test_settings_data_from_dict_missing_fields_defaults():
    s = SettingsData.from_dict({})
    assert s.visibility == "private"
    assert s.shared_with == []


def test_settings_data_from_dict_skips_malformed_users():
    d = {"visibility": "shared", "shared_with": [{"email": "ok@ok.com", "access": "view"}, {"bad": "entry"}]}
    s = SettingsData.from_dict(d)
    assert len(s.shared_with) == 1
    assert s.shared_with[0].email == "ok@ok.com"


# ── PageSettings ──────────────────────────────────────────────────────────────

@pytest.fixture
def store() -> LocalStorageBackend:
    return LocalStorageBackend()


def test_page_settings_key_root(store):
    ps = PageSettings("mysite", "", store)
    assert ps.key == "mysite/settings.json"


def test_page_settings_key_child(store):
    ps = PageSettings("mysite", "blog/posts", store)
    assert ps.key == "mysite/blog/posts/settings.json"


def test_page_settings_load_missing_returns_default(store):
    ps = PageSettings("s", "p", store)
    data = ps.load()
    assert data.visibility == "private"
    assert data.shared_with == []


def test_page_settings_load_parses_stored_json(store):
    payload = json.dumps({"visibility": "public", "shared_with": []})
    store.write("s/p/settings.json", payload)
    ps = PageSettings("s", "p", store)
    data = ps.load()
    assert data.visibility == "public"


def test_page_settings_save_writes_json(store):
    ps = PageSettings("s", "p", store)
    ps.save(SettingsData(visibility="shared", shared_with=[SharedUser("a@b.com", "view")]))
    raw = store.read("s/p/settings.json")
    d = json.loads(raw)
    assert d["visibility"] == "shared"
    assert d["shared_with"][0]["email"] == "a@b.com"


def test_page_settings_save_emits_settings_changed(store):
    ps = PageSettings("s", "p", store)
    cap = CapturingHandler()
    ps.add_handler(cap)
    ps.save(SettingsData(visibility="public"))
    assert cap.events[0].type == EventType.SETTINGS_CHANGED
    assert cap.events[0].payload["site"] == "s"
    assert cap.events[0].payload["page_path"] == "p"
    assert cap.events[0].payload["new"]["visibility"] == "public"


def test_page_settings_save_includes_old_value(store):
    ps = PageSettings("s", "p", store)
    ps.save(SettingsData(visibility="private"))
    cap = CapturingHandler()
    ps.add_handler(cap)
    ps.save(SettingsData(visibility="public"))
    payload = cap.events[0].payload
    assert payload["old"]["visibility"] == "private"
    assert payload["new"]["visibility"] == "public"


def test_page_settings_save_old_is_none_before_first_load(store):
    ps = PageSettings("s", "p", store)
    cap = CapturingHandler()
    ps.add_handler(cap)
    ps.save(SettingsData())
    assert cap.events[0].payload["old"] is None


def test_page_settings_is_event_emitter(store):
    ps = PageSettings("s", "p", store)
    assert isinstance(ps, EventEmitter)


def test_page_settings_request_access_emits_event(store):
    ps = PageSettings("s", "p", store)
    cap = CapturingHandler()
    ps.add_handler(cap)
    ps.request_access("user@example.com")
    assert cap.events[0].type == EventType.ACCESS_REQUESTED
    assert cap.events[0].payload["requester"] == "user@example.com"
    assert cap.events[0].payload["site"] == "s"
    assert cap.events[0].payload["page_path"] == "p"


# ── WikiPageMarkdownFile ──────────────────────────────────────────────────────

def test_wiki_page_path_root(store):
    f = WikiPageMarkdownFile("s", "", store)
    assert f.path == "content.md"
    assert f.key == "s/content.md"
    assert f.page_path == ""


def test_wiki_page_path_child(store):
    f = WikiPageMarkdownFile("s", "blog/posts", store)
    assert f.path == "blog/posts/content.md"
    assert f.key == "s/blog/posts/content.md"
    assert f.page_path == "blog/posts"


def test_wiki_page_write_persists(store):
    f = WikiPageMarkdownFile("s", "p", store)
    f.write("# Hello")
    assert store.read("s/p/content.md") == "# Hello"


def test_wiki_page_write_emits_page_written(store):
    f = WikiPageMarkdownFile("s", "p", store)
    cap = CapturingHandler()
    f.add_handler(cap)
    f.write("# Hello", user_id="u1")
    ev = cap.events[0]
    assert ev.type == EventType.PAGE_WRITTEN
    assert ev.payload["key"] == "s/p/content.md"
    assert ev.payload["page_path"] == "p"
    assert ev.payload["user_id"] == "u1"


def test_wiki_page_write_user_id_defaults_to_empty(store):
    f = WikiPageMarkdownFile("s", "p", store)
    cap = CapturingHandler()
    f.add_handler(cap)
    f.write("x")
    assert cap.events[0].payload["user_id"] == ""


def test_wiki_page_create_writes_content(store):
    f = WikiPageMarkdownFile("s", "p", store)
    f.create("initial")
    assert store.read("s/p/content.md") == "initial"


def test_wiki_page_create_empty_by_default(store):
    f = WikiPageMarkdownFile("s", "p", store)
    f.create()
    assert store.read("s/p/content.md") == ""


def test_wiki_page_create_emits_page_created(store):
    f = WikiPageMarkdownFile("s", "p", store)
    cap = CapturingHandler()
    f.add_handler(cap)
    f.create("init", user_id="u2")
    ev = cap.events[0]
    assert ev.type == EventType.PAGE_CREATED
    assert ev.payload["key"] == "s/p/content.md"
    assert ev.payload["user_id"] == "u2"


def test_wiki_page_delete_removes_from_storage(store):
    store.write("s/p/content.md", "data")
    f = WikiPageMarkdownFile("s", "p", store)
    f.delete()
    assert store.read("s/p/content.md") is None


def test_wiki_page_delete_emits_page_deleted(store):
    f = WikiPageMarkdownFile("s", "p", store)
    cap = CapturingHandler()
    f.add_handler(cap)
    f.delete(user_id="u3")
    ev = cap.events[0]
    assert ev.type == EventType.PAGE_DELETED
    assert ev.payload["page_path"] == "p"
    assert ev.payload["user_id"] == "u3"


def test_wiki_page_read_still_works(store):
    store.write("s/p/content.md", "content here")
    f = WikiPageMarkdownFile("s", "p", store)
    assert f.read() == "content here"


def test_wiki_page_frontmatter_accessible(store):
    content = "---\ntitle: My Page\n---\nBody."
    f = WikiPageMarkdownFile("s", "p", store, content=content)
    assert f.frontmatter == {"title": "My Page"}
    assert f.content == "Body."


# ── OnWriteEventHandler ───────────────────────────────────────────────────────

def _agent_store(*agents: tuple[str, str]) -> LocalStorageBackend:
    """Build a storage backend pre-populated with agent.md files.

    agents: sequence of (key, content) pairs.
    """
    initial = {key: content for key, content in agents}
    return LocalStorageBackend(initial)


def test_on_write_handler_ignores_non_page_written_events():
    enqueue = MagicMock()
    store = LocalStorageBackend()
    handler = OnWriteEventHandler(store, enqueue)
    from yoloscribe_io.events import Event
    handler.handle(Event(type=EventType.PAGE_READ, payload={"key": "s/p/content.md"}))
    enqueue.assert_not_called()


def test_on_write_handler_ignores_non_content_keys():
    enqueue = MagicMock()
    store = LocalStorageBackend()
    handler = OnWriteEventHandler(store, enqueue)
    from yoloscribe_io.events import Event
    handler.handle(Event(type=EventType.PAGE_WRITTEN, payload={"key": "s/p/agent.md"}))
    enqueue.assert_not_called()


def test_on_write_handler_enqueues_matching_agent():
    agent_content = "---\ntrigger: on_write\nname: syncer\n---\nDo stuff."
    store = _agent_store(("s/p/.agents/syncer/agent.md", agent_content))
    enqueue = MagicMock()
    handler = OnWriteEventHandler(store, enqueue)
    from yoloscribe_io.events import Event
    handler.handle(Event(
        type=EventType.PAGE_WRITTEN,
        payload={"key": "s/p/content.md", "user_id": "u1"},
    ))
    enqueue.assert_called_once_with("s/p/.agents/syncer/agent.md", "s/p/content.md", "u1")


def test_on_write_handler_skips_non_on_write_agent():
    agent_content = "---\ntrigger: manual\nname: manual-agent\n---\nNot triggered."
    store = _agent_store(("s/p/.agents/manual-agent/agent.md", agent_content))
    enqueue = MagicMock()
    handler = OnWriteEventHandler(store, enqueue)
    from yoloscribe_io.events import Event
    handler.handle(Event(
        type=EventType.PAGE_WRITTEN,
        payload={"key": "s/p/content.md", "user_id": "u1"},
    ))
    enqueue.assert_not_called()


def test_on_write_handler_skips_non_agent_md_files():
    store = LocalStorageBackend({"s/p/.agents/syncer/other.txt": "trigger: on_write"})
    enqueue = MagicMock()
    handler = OnWriteEventHandler(store, enqueue)
    from yoloscribe_io.events import Event
    handler.handle(Event(
        type=EventType.PAGE_WRITTEN,
        payload={"key": "s/p/content.md"},
    ))
    enqueue.assert_not_called()


def test_on_write_handler_enqueues_multiple_agents():
    store = _agent_store(
        ("s/p/.agents/a/agent.md", "---\ntrigger: on_write\n---\n"),
        ("s/p/.agents/b/agent.md", "---\ntrigger: on_write\n---\n"),
        ("s/p/.agents/c/agent.md", "---\ntrigger: manual\n---\n"),
    )
    enqueue = MagicMock()
    handler = OnWriteEventHandler(store, enqueue)
    from yoloscribe_io.events import Event
    handler.handle(Event(
        type=EventType.PAGE_WRITTEN,
        payload={"key": "s/p/content.md", "user_id": "u"},
    ))
    assert enqueue.call_count == 2


def test_on_write_handler_passes_empty_user_id_when_missing():
    agent_content = "---\ntrigger: on_write\n---\n"
    store = _agent_store(("s/p/.agents/a/agent.md", agent_content))
    enqueue = MagicMock()
    handler = OnWriteEventHandler(store, enqueue)
    from yoloscribe_io.events import Event
    handler.handle(Event(type=EventType.PAGE_WRITTEN, payload={"key": "s/p/content.md"}))
    _, _, user_id = enqueue.call_args[0]
    assert user_id == ""


def test_on_write_handler_root_page():
    """Root page key is s/content.md — no page_path prefix."""
    agent_content = "---\ntrigger: on_write\n---\n"
    store = _agent_store(("s/.agents/root-agent/agent.md", agent_content))
    enqueue = MagicMock()
    handler = OnWriteEventHandler(store, enqueue)
    from yoloscribe_io.events import Event
    # Root WikiPageMarkdownFile would emit key="s/content.md"
    # page_dir = "s", agents_prefix = "s/.agents/"
    handler.handle(Event(type=EventType.PAGE_WRITTEN, payload={"key": "s/content.md"}))
    enqueue.assert_called_once_with("s/.agents/root-agent/agent.md", "s/content.md", "")
