"""Tests for SearchBackend and related utilities (YOL-295 test infrastructure)."""
from __future__ import annotations

from agent_runner.agents.search import (
    NullSearchBackend,
    SearchResult,
    _content_key_to_page_path,
)


# ── NullSearchBackend ─────────────────────────────────────────────────────────

def test_null_search_returns_empty():
    backend = NullSearchBackend()
    assert backend.search("anything", "mysite") == []


def test_null_search_respects_limit():
    backend = NullSearchBackend()
    assert backend.search("q", "s", limit=0) == []


# ── SearchResult ──────────────────────────────────────────────────────────────

def test_search_result_defaults():
    r = SearchResult(page_path="foo/bar", excerpt="text")
    assert r.score == 0.0


def test_search_result_with_score():
    r = SearchResult(page_path="foo/bar", excerpt="text", score=0.87)
    assert r.score == 0.87


# ── _content_key_to_page_path ─────────────────────────────────────────────────

def test_converts_nested_page():
    assert _content_key_to_page_path("knuth/projects/yoloscribe/feature-backlog/content.md") == \
        "projects/yoloscribe/feature-backlog"


def test_converts_root_page():
    assert _content_key_to_page_path("knuth/content.md") == ""


def test_converts_single_level_page():
    assert _content_key_to_page_path("mysite/about/content.md") == "about"


def test_strips_only_content_md_suffix():
    # A page whose last segment is not content.md should be kept as-is
    assert _content_key_to_page_path("mysite/notes/draft.md") == "notes/draft.md"
