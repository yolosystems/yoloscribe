"""Tests for PageAgent tool surface and utilities (YOL-296)."""
from __future__ import annotations

from yoloscribe_io import WikiPageMarkdownFile
from yoloscribe_io.storage import LocalStorageBackend

from agent_runner.agents.page import PageAgent, _strip_preamble
from agent_runner.agents.search import NullSearchBackend, SearchResult
from tests.conftest import make_def, make_notify


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_agent(storage: LocalStorageBackend, page_path: str = "notes", **def_kwargs) -> PageAgent:
    wiki = WikiPageMarkdownFile(site="s", page_path=page_path, storage=storage)
    return PageAgent(
        agent_def=make_def(**def_kwargs),
        site="s",
        page_path=page_path,
        wiki=wiki,
        storage=storage,
        mcp_tools=[],
        model=None,
        user_id="u1",
        notify_fn=make_notify(),
        search=NullSearchBackend(),
    )


# ── page_read ─────────────────────────────────────────────────────────────────

def test_page_read_returns_content():
    storage = LocalStorageBackend({"s/notes/content.md": "# Hello\n"})
    agent = _make_agent(storage)
    assert agent.page_read() == "# Hello\n"


def test_page_read_missing_returns_empty():
    storage = LocalStorageBackend()
    agent = _make_agent(storage)
    assert agent.page_read() == ""


# ── page_write ────────────────────────────────────────────────────────────────

def test_page_write_persists_content():
    storage = LocalStorageBackend()
    agent = _make_agent(storage)
    result = agent.page_write("# Updated\n")
    assert result == "Content written."
    assert storage.read("s/notes/content.md") == "# Updated\n"


def test_page_write_overwrites_existing():
    storage = LocalStorageBackend({"s/notes/content.md": "# Old\n"})
    agent = _make_agent(storage)
    agent.page_write("# New\n")
    assert storage.read("s/notes/content.md") == "# New\n"


def test_page_write_does_not_touch_other_pages():
    storage = LocalStorageBackend({"s/other/content.md": "# Other\n"})
    agent = _make_agent(storage)
    agent.page_write("# Notes\n")
    assert storage.read("s/other/content.md") == "# Other\n"


# ── wiki_search ───────────────────────────────────────────────────────────────

def test_wiki_search_null_backend_returns_no_results():
    storage = LocalStorageBackend()
    agent = _make_agent(storage)
    assert agent.wiki_search("anything") == "No matching pages found."


def test_wiki_search_formats_results():
    class _FakeSearch(NullSearchBackend):
        def search(self, query, site, limit=10):
            return [SearchResult(page_path="notes/jazz", excerpt="Jazz is great.", score=0.9)]

    storage = LocalStorageBackend()
    wiki = WikiPageMarkdownFile(site="s", page_path="notes", storage=storage)
    agent = PageAgent(
        agent_def=make_def(),
        site="s",
        page_path="notes",
        wiki=wiki,
        storage=storage,
        mcp_tools=[],
        model=None,
        user_id="u1",
        notify_fn=make_notify(),
        search=_FakeSearch(),
    )
    result = agent.wiki_search("jazz")
    assert "notes/jazz" in result
    assert "Jazz is great." in result
    assert "0.900" in result


# ── _build_system_prompt ──────────────────────────────────────────────────────

def test_system_prompt_includes_description():
    storage = LocalStorageBackend()
    agent = _make_agent(storage, description="Summarise this page daily.")
    prompt = agent._build_system_prompt()
    assert "Summarise this page daily." in prompt


def test_system_prompt_includes_write_instruction():
    storage = LocalStorageBackend()
    agent = _make_agent(storage)
    prompt = agent._build_system_prompt()
    assert "page_read" in prompt
    assert "page_write" in prompt


# ── _strip_preamble ───────────────────────────────────────────────────────────

def test_strip_preamble_removes_prose_before_heading():
    raw = "Here is the updated content:\n\n# Title\n\nBody text."
    assert _strip_preamble(raw) == "# Title\n\nBody text."


def test_strip_preamble_preserves_content_starting_with_heading():
    raw = "# Title\n\nBody."
    assert _strip_preamble(raw) == "# Title\n\nBody."


def test_strip_preamble_returns_raw_when_no_heading():
    raw = "No heading here at all."
    assert _strip_preamble(raw) == "No heading here at all."


def test_strip_preamble_handles_empty_string():
    assert _strip_preamble("") == ""
