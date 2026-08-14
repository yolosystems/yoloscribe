"""The runner's half of the write reason (YOL-527).

`wiki_update` and `wiki_create` now reject a write with no stated reason, so the
runner must supply one on every path that reaches them. The failure mode this
guards is specific and nasty: it only bites on the `HttpMCPClient` path
(`AGENT_RUNNER_ACCESS=mcp`), at runtime, mid-run, as an agent_failure
notification — the `StorageMCPClient` default writes straight to S3 and would
never notice a missing reason.

`_run_reason` gets its own tests because write mode has no LLM-supplied reason
to fall back on: the agent replies with markdown rather than calling a tool, so
the reason is derived, and a derivation that silently produces "" would reopen
exactly that hole.
"""
from __future__ import annotations

import pytest
from yoloscribe_io.storage import LocalStorageBackend

from agent_runner.agents.page import _run_reason
from agent_runner.mcp_client import FakeMCPClient


class TestRunReason:
    def test_uses_the_first_line_of_the_prompt(self):
        reason = _run_reason("linear-sync", "Refresh the task table from Linear.\n\nDetails: ...")
        assert reason == "linear-sync run: Refresh the task table from Linear."

    def test_skips_leading_blank_lines(self):
        assert _run_reason("a", "\n\n  Do the thing  \n") == "a run: Do the thing"

    def test_long_first_line_is_truncated(self):
        reason = _run_reason("a", "x" * 500)
        assert reason.endswith("…")
        # Short enough to stay a one-liner in the owner's notification inbox.
        assert len(reason) < 200

    @pytest.mark.parametrize("prompt", ["", "   ", "\n\n"])
    def test_empty_prompt_still_yields_a_usable_reason(self, prompt):
        """The derived reason must never be empty — that is what the MCP tool rejects."""
        reason = _run_reason("nightly", prompt)
        assert reason == "nightly scheduled run"
        assert len(reason.strip()) >= 8


class TestClientThreadsReason:
    """The reason must survive the client hop, not just the tool signature."""

    def test_wiki_write_records_the_reason(self):
        c = FakeMCPClient()
        c.wiki_write("notes", "# X\n", "trimming the intro per review")
        assert c.reasons == [("notes", "trimming the intro per review")]

    def test_wiki_create_records_the_reason(self):
        c = FakeMCPClient()
        c.wiki_create("planning/q3", "# Q3\n", "new page for the Q3 plan")
        assert c.reasons == [("planning/q3", "new page for the Q3 plan")]

    def test_conflicted_write_records_nothing(self):
        """A write that lost the etag race did not happen, so it has no reason."""
        c = FakeMCPClient(pages={"notes": "# v1\n"})
        assert c.wiki_write("notes", "# v2\n", "stale attempt", expected_etag="etag-999") is False
        assert c.reasons == []


class TestIngestSuppliesReason:
    def test_parent_stub_names_the_page_it_was_created_for(self):
        """The stub is machine-created, so its reason is the only thing that
        explains why a page nobody asked for exists."""
        from types import SimpleNamespace

        from agent_runner.agents.ingest import IngestAgent

        # _ensure_parent_pages touches only self._mcp; constructing a real
        # IngestAgent would drag in a model and a full tool surface to test
        # three lines of path walking.
        c = FakeMCPClient()
        IngestAgent._ensure_parent_pages(
            SimpleNamespace(_mcp=c), "cooking/recipes/heritage-pork"
        )

        created = dict(c.reasons)
        assert set(created) == {"cooking", "cooking/recipes"}
        for reason in created.values():
            assert "cooking/recipes/heritage-pork" in reason


class TestStorageClientAcceptsReason:
    """The direct-S3 client must take the same argument, or the two impls diverge."""

    def test_storage_client_write_accepts_reason(self):
        from agent_runner.agents.search import NullSearchBackend
        from agent_runner.mcp_client import StorageMCPClient

        store = LocalStorageBackend()
        c = StorageMCPClient(store, "s", NullSearchBackend(), lambda *a, **k: None, "u1")

        assert c.wiki_write("notes", "# X\n", "a real reason") is True
        assert store.read("s/notes/content.md") == "# X\n"
