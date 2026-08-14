"""Caller-tier classification of the MCP tool registry (YOL-525).

Every tool declares which callers may see and invoke it. The guard that matters
most is `test_no_untagged_tools`: the tier default is fail-closed, so a tool
added without a tag silently disappears from the external surface. Without this
test the next person debugs a missing tool rather than a missing tag.

These introspect the registry only — no tool is invoked — so the clients passed
to create_mcp_app are irrelevant and left as None.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def _app():
    from mcp_server import create_mcp_app

    return create_mcp_app(
        s3_client=None,
        bucket="test-bucket",
        s3vectors_client=None,
        vectors_bucket="",
        vectors_index="",
        bedrock_embedding_model="",
        bedrock_region="us-west-2",
        auth_provider=None,
        user_site_repo=None,
        sqs_indexing_client=None,
        sqs_indexing_queue_url="",
        local_mode=True,
    )


def _tools():
    """The raw registry, with tier filtering bypassed.

    `list_tools()` runs the tier middleware, so it would return only what the
    *current* caller may see — and with no HTTP request in scope that is the
    external tier. These tests are about how tools are classified, so they read
    the unfiltered registry; what the middleware does with those tags is
    covered by test_mcp_tool_tier_filtering.py.
    """
    mcp = _app().state.fastmcp_server
    tools = asyncio.run(mcp.list_tools(run_middleware=False))
    return {t.name: set(getattr(t, "tags", None) or ()) for t in tools}


def _visible_to(tier: str) -> set[str]:
    return {name for name, tags in _tools().items() if tier in tags}


class TestTierTagging:
    def test_no_untagged_tools(self):
        """Fail-closed means an untagged tool vanishes from the external surface.

        This is the regression that would otherwise be discovered by a confused
        third-party client rather than by CI.
        """
        from mcp_server import TIER_EXTERNAL, TIER_INTERNAL

        untagged = sorted(
            name for name, tags in _tools().items()
            if not ({TIER_INTERNAL, TIER_EXTERNAL} & tags)
        )
        assert untagged == [], (
            f"tools missing a tier tag: {untagged}. Add TIER_INTERNAL and/or "
            f"TIER_EXTERNAL to their @mcp.tool(tags=...) — untagged is treated "
            f"as internal, so these are currently hidden from external callers."
        )

    def test_every_tool_is_reachable_by_someone(self):
        """A tool visible to no tier is dead weight — almost certainly a typo."""
        from mcp_server import TIER_EXTERNAL, TIER_INTERNAL

        reachable = _visible_to(TIER_INTERNAL) | _visible_to(TIER_EXTERNAL)
        assert set(_tools()) == reachable


class TestInternalSurface:
    """The run-token surface is the first-party agent-runner's.

    Narrowing it breaks agent runs, which fail asynchronously into
    `agent_failure` notifications rather than returning a status code — so a
    regression here is quiet, and worth pinning explicitly.
    """

    @pytest.mark.parametrize("tool", [
        "ingest_list_pending", "ingest_mark_processed", "ingest_read_owner_instructions",
        "ingest_read_pending", "ingest_read_pending_bytes", "ingest_write_extracted",
        "run_log_append", "propose_page_change", "notify",
        "wiki_create", "wiki_read", "wiki_update", "wiki_list", "search",
    ])
    def test_runner_dependency_stays_internal(self, tool):
        from mcp_server import TIER_INTERNAL

        assert tool in _visible_to(TIER_INTERNAL)


class TestExternalSurface:
    @pytest.mark.parametrize("tool", [
        # Librarian internals — being removed entirely with the Librarian (YOL-509).
        "read_memory", "write_memory", "read_archetypes", "write_archetypes",
        "read_signal_log", "emit_signal",
        # Agent-runner machinery. `notify` writes to platform-controlled
        # notifications.md, which is deliberately not in SAFE_PATH.
        "run_log_append", "propose_page_change", "notify", "annotate_trace",
        "ingest_write_extracted", "ingest_mark_processed",
    ])
    def test_internal_only_tools_are_hidden(self, tool):
        from mcp_server import TIER_EXTERNAL

        assert tool not in _visible_to(TIER_EXTERNAL)

    @pytest.mark.parametrize("tool", [
        "wiki_create", "wiki_read", "wiki_update", "wiki_list",
        "wiki_archive", "wiki_diff", "wiki_versions", "search",
    ])
    def test_public_surface_is_visible(self, tool):
        from mcp_server import TIER_EXTERNAL

        assert tool in _visible_to(TIER_EXTERNAL)

    def test_destructive_owner_action_is_not_given_to_agents(self):
        """empty_archive permanently deletes every archived page.

        It is an owner action initiated from the UI; no agent should hold it,
        and the runner never calls it.
        """
        from mcp_server import TIER_INTERNAL

        assert "empty_archive" not in _visible_to(TIER_INTERNAL)

    @pytest.mark.parametrize("tool", [
        "agent_create", "agent_create_page", "agent_create_ingest",
        "agent_create_notification", "agent_update", "agent_delete",
        "skill_create", "skill_update", "skill_delete",
    ])
    def test_authoring_tools_are_internal_only(self, tool):
        """YOL-526: definitions are YoloScribe's to author, not a 3P assistant's.

        These stayed registered rather than being deleted because the platform's
        own learned-agent path writes through them; what retires is external
        access. A tag slipping back to external re-opens the surface silently,
        since nothing else would fail.
        """
        from mcp_server import TIER_EXTERNAL

        assert tool not in _visible_to(TIER_EXTERNAL)

    @pytest.mark.parametrize("tool", [
        "agent_read", "agent_list", "skill_list", "skill_read", "list_skill_tools",
    ])
    def test_authoring_introspection_stays_external(self, tool):
        """Read-only introspection survives YOL-526.

        External assistants still benefit from knowing what exists — and pinning
        this stops the retirement being over-applied to the read side.
        """
        from mcp_server import TIER_EXTERNAL

        assert tool in _visible_to(TIER_EXTERNAL)
