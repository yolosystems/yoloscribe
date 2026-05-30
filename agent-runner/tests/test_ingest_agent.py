"""Tests for IngestAgent tool surface and scope enforcement (YOL-298)."""
from __future__ import annotations

from yoloscribe_io import Scope
from yoloscribe_io.storage import LocalStorageBackend

from agent_runner.agents.ingest import IngestAgent
from agent_runner.agents.search import NullSearchBackend
from tests.conftest import make_def, make_notify

SITE = "s"


def _make_agent(storage: LocalStorageBackend, max_page_reads: int = 10, **def_kwargs) -> IngestAgent:
    return IngestAgent(
        agent_def=make_def(trigger="schedule", **def_kwargs),
        site=SITE,
        page_path=".user/ingest",
        storage=storage,
        mcp_tools=[],
        model=None,
        user_id="u1",
        notify_fn=make_notify(),
        search=NullSearchBackend(),
        max_page_reads=max_page_reads,
    )


def _storage_with_kb(topics: list[str], extra: dict | None = None) -> LocalStorageBackend:
    kb_content = "".join(f"- {t}\n" for t in topics)
    data = {f"{SITE}/.user/kb-index.md": kb_content}
    if extra:
        data.update(extra)
    return LocalStorageBackend(data)


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


# ── kb_index_read ─────────────────────────────────────────────────────────────

def test_kb_index_read_no_topics():
    agent = _make_agent(LocalStorageBackend())
    result = agent.kb_index_read()
    assert "empty" in result.lower()


def test_kb_index_read_lists_topics():
    storage = _storage_with_kb(["jazz", "cooking"])
    agent = _make_agent(storage)
    result = agent.kb_index_read()
    assert "jazz" in result
    assert "cooking" in result


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


# ── wiki_write — allowed ──────────────────────────────────────────────────────

def test_wiki_write_allowed_topic():
    storage = _storage_with_kb(["jazz"])
    agent = _make_agent(storage)
    result = agent.wiki_write("jazz/miles-davis", "# Miles Davis\n")
    assert "Written" in result
    assert storage.read(f"{SITE}/jazz/miles-davis/content.md") == "# Miles Davis\n"


def test_wiki_write_empty_kb_allows_any_topic():
    # When kb-index has no topics, writes are unrestricted by topic.
    storage = LocalStorageBackend()
    agent = _make_agent(storage)
    result = agent.wiki_write("anything/here", "content")
    assert "Written" in result


# ── wiki_write — denied ───────────────────────────────────────────────────────

def test_wiki_write_denied_unknown_topic():
    storage = _storage_with_kb(["jazz"])
    agent = _make_agent(storage)
    result = agent.wiki_write("cooking/pasta", "recipe")
    assert "denied" in result.lower()
    assert "cooking" in result


def test_wiki_write_denied_by_exclude_scope():
    from yoloscribe_io import Scope
    scope = Scope(exclude=["jazz/*"])
    storage = _storage_with_kb(["jazz"])
    agent = _make_agent(storage, scope=scope)
    result = agent.wiki_write("jazz/miles-davis", "content")
    assert "denied" in result.lower()


# ── _check_scope ──────────────────────────────────────────────────────────────

def test_check_scope_no_topics_returns_none():
    agent = _make_agent(LocalStorageBackend())
    assert agent._check_scope("any/path") is None


def test_check_scope_matching_topic_returns_none():
    storage = _storage_with_kb(["jazz"])
    agent = _make_agent(storage)
    assert agent._check_scope("jazz/subpage") is None


def test_check_scope_non_matching_topic_returns_error():
    storage = _storage_with_kb(["jazz"])
    agent = _make_agent(storage)
    error = agent._check_scope("cooking/pasta")
    assert error is not None
    assert "cooking" in error


def test_check_scope_exclude_returns_error():
    scope = Scope(exclude=["private/*"])
    storage = _storage_with_kb(["private"])
    agent = _make_agent(storage, scope=scope)
    error = agent._check_scope("private/notes")
    assert error is not None


# ── system prompt ─────────────────────────────────────────────────────────────

def test_system_prompt_includes_ingest_workflow():
    agent = _make_agent(LocalStorageBackend())
    prompt = agent._build_system_prompt()
    assert "ingest_list_pending" in prompt
    assert "wiki_write" in prompt
