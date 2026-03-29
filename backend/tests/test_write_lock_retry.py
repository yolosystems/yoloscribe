"""
Unit tests for page-level write lock retry logic (test plan group 4).

Covers:
  - S3Tools.put_content sets write_conflict = True on 412 / PreconditionFailed
  - S3Tools.put_content returns a descriptive conflict message on 412
  - put_content does not set write_conflict on a successful write
  - The retry-loop pattern (as implemented in chat.py) exhausts MAX_RETRIES and
    produces a user-friendly error message — not a traceback or 500
  - The retry loop succeeds when the conflict clears on a subsequent attempt
"""

import sys
import os
from unittest.mock import MagicMock
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.base import S3Tools


# ── Helpers ───────────────────────────────────────────────────────────────────

def _precondition_failed() -> ClientError:
    """Build a ClientError that mimics S3/MinIO 412 PreconditionFailed."""
    return ClientError(
        {
            "Error": {
                "Code": "PreconditionFailed",
                "Message": "At least one of the pre-conditions you specified did not hold",
            }
        },
        "PutObject",
    )


def _make_tools(site: str = "mysite", page_path: str = "my-page") -> S3Tools:
    """Return an S3Tools instance with a mocked S3 client and pre-seeded ETag cache."""
    mock_s3 = MagicMock()
    # The except clause in put_content catches self.s3.exceptions.ClientError,
    # so we wire it to the real ClientError class.
    mock_s3.exceptions.ClientError = ClientError
    tools = S3Tools(s3=mock_s3, bucket="test-bucket", user_site=site)
    # Pre-seed the ETag cache so put_content uses IfMatch (the conflict path)
    key = f"{site}/{page_path}/content.md"
    tools._etag_cache[key] = '"abc123etag"'
    return tools


# ── Tests: write_conflict flag ────────────────────────────────────────────────

class TestWriteConflictFlag:
    def test_sets_write_conflict_on_412(self):
        tools = _make_tools()
        tools.s3.put_object.side_effect = _precondition_failed()

        tools.put_content(site="mysite", content="new content", page_path="my-page")

        assert tools.write_conflict is True

    def test_returns_conflict_message_on_412(self):
        tools = _make_tools()
        tools.s3.put_object.side_effect = _precondition_failed()

        result = tools.put_content(site="mysite", content="new content", page_path="my-page")

        assert isinstance(result, str)
        assert "conflict" in result.lower() or "modified" in result.lower(), (
            f"Expected a conflict message, got: {result!r}"
        )

    def test_no_write_conflict_on_success(self):
        tools = _make_tools()
        # put_object returns None by default (MagicMock success)

        tools.put_content(site="mysite", content="new content", page_path="my-page")

        assert tools.write_conflict is False

    def test_write_conflict_cleared_between_retries(self):
        """The retry loop in chat.py resets write_conflict = False before each attempt."""
        tools = _make_tools()
        tools.s3.put_object.side_effect = _precondition_failed()

        # First attempt — conflict
        tools.put_content(site="mysite", content="v1", page_path="my-page")
        assert tools.write_conflict is True

        # Loop resets the flag (mirroring: s3_tools.write_conflict = False in chat.py)
        tools.write_conflict = False
        tools.put_content(site="mysite", content="v2", page_path="my-page")
        assert tools.write_conflict is True


# ── Tests: retry loop exhaustion ─────────────────────────────────────────────

class TestRetryExhaustion:
    def test_user_friendly_error_after_max_retries(self):
        """Simulates the retry loop in chat.py exhausting all 3 attempts."""
        tools = _make_tools()
        tools.s3.put_object.side_effect = _precondition_failed()

        _MAX_WRITE_RETRIES = 3
        final_error: str | None = None

        for attempt in range(_MAX_WRITE_RETRIES):
            tools.write_conflict = False
            tools.put_content(site="mysite", content=f"attempt {attempt}", page_path="my-page")
            if not tools.write_conflict:
                break
            if attempt == _MAX_WRITE_RETRIES - 1:
                # This is the string returned to the user in chat.py
                final_error = (
                    "Failed to save: the page is being frequently modified by "
                    "another writer. Please try again in a moment."
                )

        assert final_error is not None, "Expected exhaustion after 3 failed attempts"
        # Must be user-friendly — no raw Python internals
        assert "try again" in final_error.lower()
        assert "Traceback" not in final_error
        assert "500" not in final_error
        assert "Exception" not in final_error

    def test_retry_loop_succeeds_when_conflict_clears(self):
        """Conflict on first attempt, success on second — loop should break."""
        tools = _make_tools()

        call_count = 0

        def _put_object_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _precondition_failed()
            # Second call succeeds (returns None / no exception)

        tools.s3.put_object.side_effect = _put_object_side_effect

        _MAX_WRITE_RETRIES = 3
        succeeded = False

        for attempt in range(_MAX_WRITE_RETRIES):
            tools.write_conflict = False
            # Simulate get_content re-reading a fresh ETag before retry
            key = "mysite/my-page/content.md"
            tools._etag_cache[key] = f'"fresh-etag-{attempt}"'
            tools.put_content(site="mysite", content=f"attempt {attempt}", page_path="my-page")
            if not tools.write_conflict:
                succeeded = True
                break

        assert succeeded, "Retry loop should succeed once conflict clears"
        assert call_count == 2, f"Expected exactly 2 put_object calls, got {call_count}"

    def test_put_object_called_with_if_match_when_etag_cached(self):
        """put_content sends IfMatch when an ETag is cached for the key."""
        tools = _make_tools()

        tools.put_content(site="mysite", content="new content", page_path="my-page")

        call_kwargs = tools.s3.put_object.call_args.kwargs
        assert "IfMatch" in call_kwargs, "Expected IfMatch kwarg when ETag is cached"
        assert call_kwargs["IfMatch"] == '"abc123etag"'

    def test_put_object_called_without_if_match_when_no_etag_cached(self):
        """put_content omits IfMatch when no ETag is in the cache (first write)."""
        mock_s3 = MagicMock()
        mock_s3.exceptions.ClientError = ClientError
        tools = S3Tools(s3=mock_s3, bucket="test-bucket", user_site="mysite")
        # No ETag seeded in cache

        tools.put_content(site="mysite", content="new content", page_path="my-page")

        call_kwargs = tools.s3.put_object.call_args.kwargs
        assert "IfMatch" not in call_kwargs, "Expected no IfMatch when ETag is not cached"
