"""Tests for the runner's MCP client layer (YOL-502).

FakeMCPClient is exercised directly (it's the test double PageAgent/IngestAgent
and the conformance harness will use); HttpMCPClient's protocol result-parsing
is unit-tested via _parse_tool_result against the shapes a Strands MCPToolResult
can take.
"""
from __future__ import annotations

import pytest

from agent_runner.mcp_client import FakeMCPClient, SearchHit, _parse_tool_result


# ── FakeMCPClient — wiki ──────────────────────────────────────────────────────

def test_wiki_read_missing_returns_empty():
    c = FakeMCPClient()
    assert c.wiki_read("nope") == ("", "")


def test_wiki_read_returns_content_and_etag():
    c = FakeMCPClient(pages={"projects/x": "# X\n"})
    content, etag = c.wiki_read("projects/x")
    assert content == "# X\n"
    assert etag


def test_wiki_write_unconditional_succeeds_and_bumps_etag():
    c = FakeMCPClient(pages={"p": "v1"})
    _, etag1 = c.wiki_read("p")
    assert c.wiki_write("p", "v2", "test write") is True
    content, etag2 = c.wiki_read("p")
    assert content == "v2"
    assert etag2 != etag1


def test_wiki_write_conditional_conflict_when_etag_stale():
    c = FakeMCPClient(pages={"p": "v1"})
    _, etag = c.wiki_read("p")
    # someone else writes, bumping the etag
    c.wiki_write("p", "v-other", "test write")
    # our conditional write with the stale etag must lose the race
    assert c.wiki_write("p", "v2", "test write", expected_etag=etag) is False
    assert c.wiki_read("p")[0] == "v-other"


def test_wiki_write_conditional_succeeds_with_fresh_etag():
    c = FakeMCPClient(pages={"p": "v1"})
    _, etag = c.wiki_read("p")
    assert c.wiki_write("p", "v2", "test write", expected_etag=etag) is True


def test_wiki_create_and_list():
    c = FakeMCPClient()
    c.wiki_create("a", "x", "test create")
    c.wiki_create("b/c", "y", "test create")
    assert c.wiki_list_pages() == ["a", "b/c"]


def test_propose_page_change_records_proposal_and_notification():
    c = FakeMCPClient()
    c.propose_page_change("projects/x", "# new\n", "structurer")
    assert c.proposals["projects/x"] == ("# new\n", "structurer")
    assert ("confirm_page_change", {"page_path": "projects/x", "agent": "structurer"}) in c.notifications


def test_search_returns_injected_hits_capped_by_limit():
    hits = [SearchHit("a", 0.9, "ex-a"), SearchHit("b", 0.8, "ex-b")]
    c = FakeMCPClient(search_results=hits)
    assert c.search("q", limit=1) == [hits[0]]


# ── FakeMCPClient — ingest ────────────────────────────────────────────────────

def test_ingest_list_read_and_mark_processed():
    c = FakeMCPClient(ingest_files={"note.md": b"hello", "img.png": b"\x89PNG"})
    assert c.ingest_list_pending() == ["img.png", "note.md"]
    assert c.ingest_read("note.md") == "hello"
    assert c.ingest_read_bytes("img.png") == b"\x89PNG"
    c.ingest_mark_processed("note.md")
    assert c.ingest_list_pending() == ["img.png"]  # note.md moved out of pending
    assert c.ingest_read("note.md") is None


def test_ingest_read_missing_returns_none():
    assert FakeMCPClient().ingest_read("nope") is None
    assert FakeMCPClient().ingest_read_bytes("nope") is None


def test_ingest_owner_instructions_and_extracted():
    c = FakeMCPClient(ingest_files={"r.pdf": b"%PDF"}, owner_instructions="meetings go under meetings/")
    assert c.ingest_read_owner_instructions() == "meetings go under meetings/"
    c.ingest_write_extracted("r.pdf", "# extracted\n")
    assert c._extracted["r.pdf"] == "# extracted\n"


def test_notify_records_events():
    c = FakeMCPClient()
    c.notify("ingest_end", {"summary": "did stuff"})
    assert c.notifications == [("ingest_end", {"summary": "did stuff"})]


# ── HttpMCPClient result parsing ──────────────────────────────────────────────
# Shapes below mirror the real Strands MCPToolResult verified live against the
# backend: a dict with {status, content, structuredContent, isError}, where
# content blocks are {"text": ...} (no "type" key) and structuredContent holds
# the FastMCP return (a bare value wrapped as {"result": ...}).

def test_parse_structured_content_unwraps_wrapped_result():
    res = {"status": "success", "isError": False, "structuredContent": {"result": {"content": "hi", "etag": "e1"}}}
    assert _parse_tool_result("wiki_read", res) == {"content": "hi", "etag": "e1"}


def test_parse_structured_content_plain_dict():
    res = {"status": "success", "isError": False,
           "structuredContent": {"content": "hi", "etag": '"abc"'}}
    assert _parse_tool_result("wiki_read", res) == {"content": "hi", "etag": '"abc"'}


def test_parse_text_json_block_when_no_structured():
    res = {"status": "success", "isError": False, "structuredContent": None,
           "content": [{"text": '{"conflict": true}'}]}
    assert _parse_tool_result("wiki_update", res) == {"conflict": True}


def test_parse_plain_text_fallback():
    res = {"status": "success", "isError": False, "structuredContent": None,
           "content": [{"text": "not json"}]}
    assert _parse_tool_result("x", res) == "not json"


def test_parse_error_raises():
    res = {"status": "error", "isError": True, "content": [{"text": "scope denied"}]}
    with pytest.raises(RuntimeError, match="scope denied"):
        _parse_tool_result("wiki_update", res)
