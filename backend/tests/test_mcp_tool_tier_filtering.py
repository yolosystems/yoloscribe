"""Tier filtering and enforcement at request time (YOL-546).

test_mcp_tool_tiers.py checks how tools are *classified*. This checks what the
middleware does with that classification: hide out-of-tier tools from listings,
and refuse them at call time in a way that does not reveal they exist.

The caller tier is read from `request.state.mcp_user`, which `_MCPAuthMiddleware`
populates before anything else runs. These tests stand in a fake request rather
than minting real tokens — the token-to-user mapping is already covered by
test_mcp_auth_middleware.py, and duplicating it here would test that instead of
the filtering.
"""

import asyncio
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


def _build():
    from mcp_server import create_mcp_app

    app = create_mcp_app(
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
    return app.state.fastmcp_server


@contextmanager
def _as_caller(*, run_token: bool):
    """Patch the request accessor the tier check reads.

    A run token carries a path_scope; JWT and `as_`-key callers do not — that
    single attribute is what distinguishes the tiers.
    """
    import mcp_server

    user = SimpleNamespace(
        user_id="u1",
        email=None,
        site="acme",
        path_scope=[] if run_token else None,
    )
    request = SimpleNamespace(state=SimpleNamespace(mcp_user=user))
    original = mcp_server.get_http_request
    mcp_server.get_http_request = lambda: request
    try:
        yield
    finally:
        mcp_server.get_http_request = original


@contextmanager
def _as_unidentified_caller():
    """No HTTP request in scope — get_http_request raises."""
    import mcp_server

    def _boom():
        raise RuntimeError("no active HTTP request")

    original = mcp_server.get_http_request
    mcp_server.get_http_request = _boom
    try:
        yield
    finally:
        mcp_server.get_http_request = original


def _listed(mcp) -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


class TestListingIsFilteredByTier:
    def test_external_caller_cannot_see_internal_tools(self):
        from mcp_server import TIER_EXTERNAL

        mcp = _build()
        with _as_caller(run_token=False):
            listed = _listed(mcp)

        raw = asyncio.run(mcp.list_tools(run_middleware=False))
        expected = {t.name for t in raw if TIER_EXTERNAL in (t.tags or set())}
        # Compare the whole set: a membership check would pass even if the
        # filter dropped everything, or nothing.
        assert listed == expected
        assert "notify" not in listed
        assert "write_memory" not in listed

    def test_internal_caller_sees_the_full_runner_surface(self):
        from mcp_server import TIER_INTERNAL

        mcp = _build()
        with _as_caller(run_token=True):
            listed = _listed(mcp)

        raw = asyncio.run(mcp.list_tools(run_middleware=False))
        expected = {t.name for t in raw if TIER_INTERNAL in (t.tags or set())}
        assert listed == expected
        # Narrowing these breaks agent runs, which fail asynchronously into
        # agent_failure notifications rather than returning a status code.
        for tool in ("ingest_read_pending", "run_log_append", "propose_page_change",
                     "notify", "wiki_update", "search"):
            assert tool in listed

    def test_the_two_tiers_actually_differ(self):
        """Guards against a filter that is accidentally a no-op."""
        mcp = _build()
        with _as_caller(run_token=True):
            internal = _listed(mcp)
        with _as_caller(run_token=False):
            external = _listed(mcp)

        assert internal != external
        assert "notify" in internal - external
        assert "empty_archive" in external - internal

    def test_unidentified_caller_gets_the_smaller_surface(self):
        """An unresolvable caller is not a reason to hand out internal tooling."""
        mcp = _build()
        with _as_unidentified_caller():
            listed = _listed(mcp)
        with _as_caller(run_token=False):
            external = _listed(mcp)

        assert listed == external


class TestCallTimeEnforcement:
    """Hiding is not securing — a name can still be invoked directly."""

    def test_external_caller_cannot_invoke_an_internal_tool(self):
        from fastmcp.exceptions import NotFoundError

        mcp = _build()
        with _as_caller(run_token=False):
            with pytest.raises(NotFoundError) as exc:
                asyncio.run(mcp._call_tool_mcp("write_memory", {}))
        assert "write_memory" in str(exc.value)

    def test_rejection_is_indistinguishable_from_a_missing_tool(self):
        """A distinct 'forbidden' would confirm the tool exists."""
        from fastmcp.exceptions import NotFoundError

        mcp = _build()
        with _as_caller(run_token=False):
            with pytest.raises(NotFoundError) as hidden:
                asyncio.run(mcp._call_tool_mcp("write_memory", {}))
            with pytest.raises(NotFoundError) as absent:
                asyncio.run(mcp._call_tool_mcp("no_such_tool_at_all", {}))

        # Same exception type, and the same message shape with only the name differing.
        assert str(hidden.value).replace("write_memory", "X") == \
               str(absent.value).replace("no_such_tool_at_all", "X")

    def test_internal_caller_is_not_blocked(self):
        """The block must be tier-based, not a blanket refusal.

        write_memory reaches its body and fails on the None storage backend —
        which is the point: it got past the tier check.
        """
        from fastmcp.exceptions import NotFoundError

        mcp = _build()
        with _as_caller(run_token=True):
            with pytest.raises(Exception) as exc:
                asyncio.run(mcp._call_tool_mcp("write_memory", {}))
        assert not isinstance(exc.value, NotFoundError)
