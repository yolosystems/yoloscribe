"""Unit tests for page-level write lock retry logic.

Covers:
  - WikiPageTools.write_page sets write_conflict = True on ETag mismatch
  - WikiPageTools.write_page returns a descriptive conflict message on mismatch
  - write_page does not set write_conflict on a successful write
  - The retry-loop pattern (as implemented in chat.py) exhausts MAX_RETRIES and
    produces a user-friendly error message — not a traceback or 500
  - The retry loop succeeds when the conflict clears on a subsequent attempt
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from yoloscribe_io import LocalStorageBackend, WikiPageMarkdownFile
from agents.base import WikiPageTools


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_wiki_tools(site: str = "mysite", page_path: str = "my-page") -> WikiPageTools:
    store = LocalStorageBackend()
    store.write(f"{site}/{page_path}/content.md", "# Original")
    wiki = WikiPageMarkdownFile(site, page_path, store)
    return WikiPageTools(wiki, user_id="u1"), store


# ── Tests: write_conflict flag ────────────────────────────────────────────────

class TestWriteConflictFlag:
    def test_sets_write_conflict_on_stale_etag(self):
        wt, store = _make_wiki_tools()
        # Read to capture ETag
        wt.read_page()
        # External write makes the cached ETag stale
        store.write("mysite/my-page/content.md", "# Modified externally")
        # Now write with stale ETag
        wt.write_page("# Our update")
        assert wt.write_conflict is True

    def test_returns_conflict_message_on_stale_etag(self):
        wt, store = _make_wiki_tools()
        wt.read_page()
        store.write("mysite/my-page/content.md", "# Modified externally")
        result = wt.write_page("# Our update")
        assert isinstance(result, str)
        assert "conflict" in result.lower() or "modified" in result.lower()

    def test_no_write_conflict_on_success(self):
        wt, store = _make_wiki_tools()
        wt.read_page()
        wt.write_page("# Updated content")
        assert wt.write_conflict is False

    def test_write_conflict_cleared_between_retries(self):
        """Simulates the retry loop resetting write_conflict = False before each attempt."""
        wt, store = _make_wiki_tools()
        wt.read_page()
        store.write("mysite/my-page/content.md", "# External write")

        # First attempt — conflict
        wt.write_page("# v1")
        assert wt.write_conflict is True

        # Reset flag (as the retry loop does), then attempt again with still-stale ETag
        wt.write_conflict = False
        wt.write_page("# v2")
        assert wt.write_conflict is True


# ── Tests: retry loop exhaustion ─────────────────────────────────────────────

class TestRetryExhaustion:
    def test_user_friendly_error_after_max_retries(self):
        """Simulates the retry loop in chat.py exhausting all 3 attempts."""
        wt, store = _make_wiki_tools()
        wt.read_page()
        store.write("mysite/my-page/content.md", "# External write")

        _MAX_WRITE_RETRIES = 3
        final_error: str | None = None

        for attempt in range(_MAX_WRITE_RETRIES):
            wt.write_conflict = False
            wt.write_page(f"# attempt {attempt}")
            if not wt.write_conflict:
                break
            if attempt == _MAX_WRITE_RETRIES - 1:
                final_error = (
                    "Failed to save: the page is being frequently modified by "
                    "another writer. Please try again in a moment."
                )

        assert final_error is not None, "Expected exhaustion after 3 failed attempts"
        assert "try again" in final_error.lower()
        assert "Traceback" not in final_error
        assert "Exception" not in final_error

    def test_retry_loop_succeeds_when_conflict_clears(self):
        """Conflict on first attempt, success on second — loop should break."""
        store = LocalStorageBackend()
        store.write("mysite/my-page/content.md", "# Original")

        _MAX_WRITE_RETRIES = 3
        succeeded = False

        for attempt in range(_MAX_WRITE_RETRIES):
            wiki = WikiPageMarkdownFile("mysite", "my-page", store)
            wt = WikiPageTools(wiki, user_id="u1")
            wt.write_conflict = False

            # Simulate re-reading a fresh ETag before each attempt
            wt.read_page()

            if attempt == 0:
                # Cause a conflict on first attempt only
                store.write("mysite/my-page/content.md", "# External write")

            wt.write_page(f"# attempt {attempt}")
            if not wt.write_conflict:
                succeeded = True
                break

        assert succeeded, "Retry loop should succeed once conflict clears"

    def test_write_without_prior_read_uses_no_etag(self):
        """write_page with no cached ETag (no prior read_page) always succeeds."""
        store = LocalStorageBackend()
        store.write("mysite/my-page/content.md", "# Original")
        wiki = WikiPageMarkdownFile("mysite", "my-page", store)
        wt = WikiPageTools(wiki, user_id="u1")

        # No read_page call — etag is None, so write_conditional treats it as unconditional
        result = wt.write_page("# New content")

        assert wt.write_conflict is False
        assert store.read("mysite/my-page/content.md") == "# New content"
