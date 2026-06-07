"""Tests for IngestAgent tool surface and scope enforcement."""
from __future__ import annotations

from yoloscribe_io import Scope
from yoloscribe_io.storage import LocalStorageBackend

from agent_runner.agents.ingest import IngestAgent
from agent_runner.agents.search import NullSearchBackend
from tests.conftest import make_def, make_notify

SITE = "s"


def _make_agent(
    storage: LocalStorageBackend,
    max_page_reads: int = 10,
    notify_fn=None,
    **def_kwargs,
) -> IngestAgent:
    return IngestAgent(
        agent_def=make_def(trigger="schedule", **def_kwargs),
        site=SITE,
        page_path=".user/ingest",
        storage=storage,
        mcp_tools=[],
        model=None,
        user_id="u1",
        notify_fn=notify_fn or make_notify(),
        search=NullSearchBackend(),
        max_page_reads=max_page_reads,
    )


# ── ingest_list_pending ───────────────────────────────────────────────────────

def test_list_pending_no_files():
    storage = LocalStorageBackend()
    agent = _make_agent(storage)
    assert agent.ingest_list_pending() == "No pending files."


def test_list_pending_returns_filenames():
    storage = LocalStorageBackend({
        f"{SITE}/.user/ingest/note1.md": "content",
        f"{SITE}/.user/ingest/note2.md": "content",
    })
    agent = _make_agent(storage)
    result = agent.ingest_list_pending()
    assert "note1.md" in result
    assert "note2.md" in result


def test_list_pending_excludes_content_md():
    storage = LocalStorageBackend({
        f"{SITE}/.user/ingest/content.md": "page content",
        f"{SITE}/.user/ingest/note.md": "a note",
    })
    agent = _make_agent(storage)
    result = agent.ingest_list_pending()
    assert "content.md" not in result
    assert "note.md" in result


def test_list_pending_excludes_processed():
    storage = LocalStorageBackend({
        f"{SITE}/.user/ingest/note.md": "pending",
        f"{SITE}/.user/ingest/processed/old.md": "done",
    })
    agent = _make_agent(storage)
    result = agent.ingest_list_pending()
    assert "note.md" in result
    assert "old.md" not in result


def test_list_pending_excludes_agent_files():
    storage = LocalStorageBackend({
        f"{SITE}/.user/ingest/.agents/ingest-agent/agent.md": "---\ntrigger: schedule\nname: x\n---\n",
        f"{SITE}/.user/ingest/note.md": "pending",
    })
    agent = _make_agent(storage)
    result = agent.ingest_list_pending()
    assert "agent.md" not in result
    assert "note.md" in result


# ── ingest_read ───────────────────────────────────────────────────────────────

def test_ingest_read_returns_content():
    storage = LocalStorageBackend({f"{SITE}/.user/ingest/note.md": "# Note\n"})
    agent = _make_agent(storage)
    assert agent.ingest_read("note.md") == "# Note\n"


def test_ingest_read_strips_leading_slash():
    storage = LocalStorageBackend({f"{SITE}/.user/ingest/note.md": "content"})
    agent = _make_agent(storage)
    assert agent.ingest_read("/note.md") == "content"


def test_ingest_read_missing_file():
    agent = _make_agent(LocalStorageBackend())
    assert "not found" in agent.ingest_read("missing.md").lower()


# ── ingest_mark_processed ─────────────────────────────────────────────────────

def test_mark_processed_moves_file():
    storage = LocalStorageBackend({f"{SITE}/.user/ingest/note.md": "body"})
    agent = _make_agent(storage)
    result = agent.ingest_mark_processed("note.md")
    assert "processed" in result.lower()
    assert storage.read(f"{SITE}/.user/ingest/note.md") is None
    assert storage.read(f"{SITE}/.user/ingest/processed/note.md") == "body"


def test_mark_processed_missing_file():
    agent = _make_agent(LocalStorageBackend())
    result = agent.ingest_mark_processed("ghost.md")
    assert "not found" in result.lower()


# ── wiki_list_pages ───────────────────────────────────────────────────────────

def test_wiki_list_pages_no_pages():
    agent = _make_agent(LocalStorageBackend())
    assert "No wiki pages found" in agent.wiki_list_pages()


def test_wiki_list_pages_returns_paths():
    storage = LocalStorageBackend({
        f"{SITE}/jazz/content.md": "jazz",
        f"{SITE}/cooking/content.md": "cooking",
        f"{SITE}/content.md": "root",
    })
    agent = _make_agent(storage)
    result = agent.wiki_list_pages()
    assert "jazz" in result
    assert "cooking" in result
    assert "(root)" in result


def test_wiki_list_pages_excludes_system_paths():
    storage = LocalStorageBackend({
        f"{SITE}/jazz/content.md": "jazz",
        f"{SITE}/.user/notifications.md": "notifs",
        f"{SITE}/.archive/old/content.md": "archived",
        f"{SITE}/jazz/.agents/sync/agent.md": "agent",
    })
    agent = _make_agent(storage)
    result = agent.wiki_list_pages()
    assert "jazz" in result
    assert ".user" not in result
    assert ".archive" not in result
    assert ".agents" not in result


# ── notify_owner ──────────────────────────────────────────────────────────────

def test_notify_owner_calls_notify_fn():
    events: list[tuple] = []

    def capture(event_type, payload, user_id):
        events.append((event_type, payload, user_id))

    agent = _make_agent(LocalStorageBackend(), notify_fn=capture)
    result = agent.notify_owner("Cannot route this file — no matching topic found.")
    assert events == [("ingest_unrouted", {"message": "Cannot route this file — no matching topic found."}, "u1")]
    assert "notified" in result.lower()


def test_notify_owner_instructs_leave_unprocessed():
    agent = _make_agent(LocalStorageBackend())
    result = agent.notify_owner("unclear content")
    assert "unprocessed" in result.lower()


# ── wiki_read ─────────────────────────────────────────────────────────────────

def test_wiki_read_returns_content():
    storage = LocalStorageBackend({f"{SITE}/jazz/content.md": "# Jazz\n"})
    agent = _make_agent(storage)
    assert agent.wiki_read("jazz") == "# Jazz\n"


def test_wiki_read_enforces_limit():
    storage = LocalStorageBackend({f"{SITE}/jazz/content.md": "content"})
    agent = _make_agent(storage, max_page_reads=2)
    agent.wiki_read("jazz")
    agent.wiki_read("jazz")
    result = agent.wiki_read("jazz")
    assert "limit" in result.lower()


def test_wiki_read_counter_increments():
    storage = LocalStorageBackend({f"{SITE}/jazz/content.md": "content"})
    agent = _make_agent(storage, max_page_reads=5)
    agent.wiki_read("jazz")
    agent.wiki_read("jazz")
    assert agent._read_counter[0] == 2


# ── wiki_write ────────────────────────────────────────────────────────────────

def test_wiki_write_any_page():
    storage = LocalStorageBackend()
    agent = _make_agent(storage)
    result = agent.wiki_write("jazz/miles-davis", "# Miles Davis\n")
    assert "Written" in result
    assert storage.read(f"{SITE}/jazz/miles-davis/content.md") == "# Miles Davis\n"


def test_wiki_write_new_topic_no_restriction():
    storage = LocalStorageBackend()
    agent = _make_agent(storage)
    result = agent.wiki_write("cooking/pasta", "recipe")
    assert "Written" in result


def test_wiki_write_denied_by_exclude_scope():
    scope = Scope(exclude=["jazz/*"])
    agent = _make_agent(LocalStorageBackend(), scope=scope)
    result = agent.wiki_write("jazz/miles-davis", "content")
    assert "denied" in result.lower()


# ── _check_scope ──────────────────────────────────────────────────────────────

def test_check_scope_any_path_allowed():
    agent = _make_agent(LocalStorageBackend())
    assert agent._check_scope("any/path") is None
    assert agent._check_scope("completely/different/topic") is None


def test_check_scope_exclude_returns_error():
    scope = Scope(exclude=["private/*"])
    agent = _make_agent(LocalStorageBackend(), scope=scope)
    error = agent._check_scope("private/notes")
    assert error is not None


# ── owner instructions page ───────────────────────────────────────────────────

def test_read_owner_instructions_missing_page():
    agent = _make_agent(LocalStorageBackend())
    assert agent._read_owner_instructions() == ""


def test_read_owner_instructions_returns_content():
    storage = LocalStorageBackend({
        f"{SITE}/.user/ingest/content.md": "Meeting notes go under meetings/\n",
    })
    agent = _make_agent(storage)
    assert agent._read_owner_instructions() == "Meeting notes go under meetings/"


def test_system_prompt_includes_instructions_when_set():
    agent = _make_agent(LocalStorageBackend())
    agent._owner_instructions = "Always route articles to the articles/ section."
    prompt = agent._build_system_prompt()
    assert "Always route articles to the articles/ section." in prompt
    assert "priority" in prompt.lower()


def test_system_prompt_omits_instructions_section_when_empty():
    agent = _make_agent(LocalStorageBackend())
    agent._owner_instructions = ""
    prompt = agent._build_system_prompt()
    assert "site owner" not in prompt.lower()


# ── system prompt ─────────────────────────────────────────────────────────────

def test_system_prompt_includes_search_driven_workflow():
    agent = _make_agent(LocalStorageBackend())
    prompt = agent._build_system_prompt()
    assert "wiki_search" in prompt
    assert "wiki_list_pages" in prompt
    assert "wiki_write" in prompt
    assert "notify_owner" in prompt
    assert "ingest_list_pending" in prompt
