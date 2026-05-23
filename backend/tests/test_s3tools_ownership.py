"""Unit tests for cross-site ownership enforcement.

After the yoloscribe-io migration, site-scoping is enforced at two levels:

1. WikiPageTools and SiteTools bind site at construction time — there are no
   site parameters on individual tool methods, so the LLM cannot be injected
   with a different target site.

2. ChatAgent.run() guards against cross-site requests by checking
   user_site == site before any tools are invoked.

This file tests both levels.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock

from yoloscribe_io import LocalStorageBackend, WikiPageMarkdownFile
from agents.base import SiteTools, WikiPageTools


# ── Helpers ───────────────────────────────────────────────────────────────────

def _site_tools(site: str = "alice") -> SiteTools:
    store = LocalStorageBackend()
    return SiteTools(site, store, user_id="u1")


def _wiki_tools(site: str = "alice", page_path: str = "") -> WikiPageTools:
    store = LocalStorageBackend()
    wiki = WikiPageMarkdownFile(site, page_path, store)
    return WikiPageTools(wiki, user_id="u1")


# ── WikiPageTools: site is bound at construction ──────────────────────────────

class TestWikiPageToolsSiteBound:
    def test_site_property_reflects_construction_site(self):
        wt = _wiki_tools("alice")
        assert wt.site == "alice"

    def test_page_path_property_reflects_construction_path(self):
        wt = _wiki_tools("alice", "my-page")
        assert wt.page_path == "my-page"

    def test_read_page_reads_from_bound_site(self):
        store = LocalStorageBackend()
        store.write("alice/content.md", "# Alice root")
        store.write("bob/content.md", "# Bob root")
        wiki = WikiPageMarkdownFile("alice", "", store)
        wt = WikiPageTools(wiki)
        assert wt.read_page() == "# Alice root"

    def test_write_page_writes_to_bound_site(self):
        store = LocalStorageBackend()
        store.write("alice/content.md", "# Original")
        wiki = WikiPageMarkdownFile("alice", "", store)
        wt = WikiPageTools(wiki)
        wt.read_page()
        wt.write_page("# Updated")
        assert store.read("alice/content.md") == "# Updated"
        assert store.read("bob/content.md") is None


# ── SiteTools: site is bound at construction ──────────────────────────────────

class TestSiteToolsSiteBound:
    def test_site_property_reflects_construction_site(self):
        st = _site_tools("alice")
        assert st.site == "alice"

    def test_list_skills_reads_from_bound_site(self):
        store = LocalStorageBackend()
        store.write("alice/.skills/summariser/SKILL.md", "---\ndescription: Summarise pages\ntools:\n  - linear\n---\nBody.")
        st = SiteTools("alice", store)
        result = st.list_skills()
        assert "summariser" in result
        assert "Summarise pages" in result

    def test_list_skills_does_not_see_other_site(self):
        store = LocalStorageBackend()
        store.write("bob/.skills/other/SKILL.md", "---\ndescription: Bob skill\n---\n")
        st = SiteTools("alice", store)
        result = st.list_skills()
        assert "other" not in result
        assert "bob" not in result

    def test_create_agent_writes_to_bound_site(self):
        store = LocalStorageBackend()
        st = SiteTools("alice", store)
        result = st.create_agent(
            agent_name="my-agent",
            description="Does stuff",
            skills=[],
        )
        assert "created" in result
        assert store.read("alice/.agents/my-agent/agent.md") is not None
        assert store.read("bob/.agents/my-agent/agent.md") is None

    def test_create_page_writes_to_bound_site(self):
        store = LocalStorageBackend()
        st = SiteTools("alice", store)
        result = st.create_page("new-page")
        assert "created" in result
        assert store.read("alice/new-page/content.md") is not None
        assert store.read("bob/new-page/content.md") is None


# ── ChatAgent.run(): cross-site guard ─────────────────────────────────────────

class TestChatAgentCrossSiteGuard:
    def _make_chat_agent(self):
        from agents.chat import ChatAgent
        mock_s3 = MagicMock()
        return ChatAgent(s3=mock_s3, bucket="test-bucket")

    def test_mismatched_site_raises_permission_error(self):
        agent = self._make_chat_agent()
        with pytest.raises(PermissionError, match="alice"):
            agent.run(
                message="do something",
                current_content="",
                history=[],
                site="eve",
                user_site="alice",
            )

    def test_matching_site_passes_guard(self):
        agent = self._make_chat_agent()
        try:
            agent.run(
                message="hello",
                current_content="",
                history=[],
                site="alice",
                user_site="alice",
            )
        except PermissionError as exc:
            pytest.fail(f"Should not raise PermissionError when sites match: {exc}")
        except Exception:
            pass  # Expected — no real LLM client in tests

    def test_empty_user_site_skips_check(self):
        """Internal/unauthenticated callers pass user_site='' — should not raise."""
        agent = self._make_chat_agent()
        try:
            agent.run(
                message="hello",
                current_content="",
                history=[],
                site="any-site",
                user_site="",
            )
        except PermissionError as exc:
            pytest.fail(f"Should not raise PermissionError for empty user_site: {exc}")
        except Exception:
            pass  # Expected — no real LLM client in tests
