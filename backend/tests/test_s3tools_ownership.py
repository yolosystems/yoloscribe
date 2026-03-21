"""Unit tests for S3Tools cross-site ownership enforcement (YOL-39).

Every method guarded by _require_site_ownership / _require_read_access is
tested to confirm:
  - Calls with the correct site succeed (S3 client is invoked).
  - Calls with a *different* site raise PermissionError *before* any S3
    client method is invoked (the mock should never be called).
  - When user_site is None (internal / unauthenticated path), the check is
    skipped and the S3 call proceeds normally.
"""

from unittest.mock import MagicMock, patch
import json
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import sys
import os

# Ensure the backend package root is on the path so we can import agents.base
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.base import S3Tools


def _make_tools(user_site: str | None = "alice", user_email: str | None = None) -> S3Tools:
    mock_s3 = MagicMock()
    return S3Tools(s3=mock_s3, bucket="test-bucket", user_site=user_site, user_email=user_email)


# ---------------------------------------------------------------------------
# _require_site_ownership
# ---------------------------------------------------------------------------


class TestRequireSiteOwnership:
    def test_own_site_passes(self):
        tools = _make_tools("alice")
        tools._require_site_ownership("alice")  # must not raise

    def test_other_site_raises(self):
        tools = _make_tools("alice")
        with pytest.raises(PermissionError, match="bob"):
            tools._require_site_ownership("bob")

    def test_no_user_site_skips_check(self):
        tools = _make_tools(user_site=None)
        tools._require_site_ownership("anyone")  # must not raise


# ---------------------------------------------------------------------------
# _require_read_access
# ---------------------------------------------------------------------------


class TestRequireReadAccess:
    def test_own_site_passes(self):
        tools = _make_tools("alice")
        tools._require_read_access("alice", "")  # must not raise; no S3 call needed
        tools.s3.get_object.assert_not_called()

    def test_no_user_site_skips_check(self):
        tools = _make_tools(user_site=None)
        tools._require_read_access("anyone", "")  # must not raise
        tools.s3.get_object.assert_not_called()

    def test_other_site_public_page_passes(self):
        tools = _make_tools("alice")
        tools.s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps({"visibility": "public", "shared_with": []}).encode())
        }
        tools._require_read_access("bob", "")  # public — must not raise

    def test_other_site_private_page_raises(self):
        tools = _make_tools("alice")
        tools.s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps({"visibility": "private", "shared_with": []}).encode())
        }
        with pytest.raises(PermissionError):
            tools._require_read_access("bob", "")

    def test_other_site_shared_page_with_matching_email_passes(self):
        tools = _make_tools("alice", user_email="alice@example.com")
        tools.s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps({
                "visibility": "shared",
                "shared_with": [{"email": "alice@example.com", "access": "view"}],
            }).encode())
        }
        tools._require_read_access("bob", "page1")  # must not raise

    def test_other_site_shared_page_without_email_raises(self):
        tools = _make_tools("alice", user_email=None)
        tools.s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: json.dumps({
                "visibility": "shared",
                "shared_with": [{"email": "alice@example.com", "access": "view"}],
            }).encode())
        }
        with pytest.raises(PermissionError):
            tools._require_read_access("bob", "page1")

    def test_other_site_settings_missing_raises(self):
        tools = _make_tools("alice")
        tools.s3.get_object.side_effect = Exception("NoSuchKey")
        with pytest.raises(PermissionError):
            tools._require_read_access("bob", "")


# ---------------------------------------------------------------------------
# get_content
# ---------------------------------------------------------------------------


class TestGetContent:
    def test_own_site_calls_s3(self):
        tools = _make_tools("alice")
        tools.s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"# Hello")}
        result = tools.get_content(site="alice")
        tools.s3.get_object.assert_called_once()
        assert result == "# Hello"

    def test_cross_site_raises_before_s3(self):
        # get_object IS called once to read settings.json (for the visibility
        # fallback check), but the content key must never be fetched.
        tools = _make_tools("alice")
        with pytest.raises(PermissionError):
            tools.get_content(site="bob")
        # Verify the content key was never fetched — only the settings.json read.
        calls = [str(c) for c in tools.s3.get_object.call_args_list]
        assert not any("content.md" in c for c in calls)

    def test_no_user_site_calls_s3(self):
        tools = _make_tools(user_site=None)
        tools.s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"# Hello")}
        tools.get_content(site="anyone")
        tools.s3.get_object.assert_called_once()


# ---------------------------------------------------------------------------
# put_content
# ---------------------------------------------------------------------------


class TestPutContent:
    def test_own_site_calls_s3(self):
        tools = _make_tools("alice")
        tools.put_content(site="alice", content="# Hello")
        tools.s3.put_object.assert_called_once()

    def test_cross_site_raises_before_s3(self):
        tools = _make_tools("alice")
        with pytest.raises(PermissionError):
            tools.put_content(site="bob", content="# Hello")
        tools.s3.put_object.assert_not_called()

    def test_no_user_site_calls_s3(self):
        tools = _make_tools(user_site=None)
        tools.put_content(site="anyone", content="# Hello")
        tools.s3.put_object.assert_called_once()


# ---------------------------------------------------------------------------
# list_skills
# ---------------------------------------------------------------------------


class TestListSkills:
    def test_own_site_calls_s3(self):
        tools = _make_tools("alice")
        tools.s3.list_objects_v2.return_value = {"CommonPrefixes": []}
        tools.list_skills(site="alice")
        tools.s3.list_objects_v2.assert_called_once()

    def test_cross_site_raises_before_s3(self):
        tools = _make_tools("alice")
        with pytest.raises(PermissionError):
            tools.list_skills(site="bob")
        tools.s3.list_objects_v2.assert_not_called()


# ---------------------------------------------------------------------------
# list_agents
# ---------------------------------------------------------------------------


class TestListAgents:
    def test_own_site_calls_s3(self):
        tools = _make_tools("alice")
        tools.s3.list_objects_v2.return_value = {"CommonPrefixes": []}
        tools.list_agents(site="alice")
        tools.s3.list_objects_v2.assert_called_once()

    def test_cross_site_raises_before_s3(self):
        tools = _make_tools("alice")
        with pytest.raises(PermissionError):
            tools.list_agents(site="bob")
        tools.s3.list_objects_v2.assert_not_called()


# ---------------------------------------------------------------------------
# put_agent
# ---------------------------------------------------------------------------


class TestPutAgent:
    def test_own_site_calls_s3(self):
        tools = _make_tools("alice")
        tools.put_agent(site="alice", agent_name="my-agent", description="Test", skills=[])
        tools.s3.put_object.assert_called_once()

    def test_cross_site_raises_before_s3(self):
        tools = _make_tools("alice")
        with pytest.raises(PermissionError):
            tools.put_agent(site="bob", agent_name="my-agent", description="Test", skills=[])
        tools.s3.put_object.assert_not_called()


# ---------------------------------------------------------------------------
# put_skill
# ---------------------------------------------------------------------------


class TestPutSkill:
    def test_own_site_calls_s3(self):
        tools = _make_tools("alice")
        tools.put_skill(site="alice", skill_name="my-skill", markdown="---\n---\n")
        tools.s3.put_object.assert_called_once()

    def test_cross_site_raises_before_s3(self):
        tools = _make_tools("alice")
        with pytest.raises(PermissionError):
            tools.put_skill(site="bob", skill_name="my-skill", markdown="---\n---\n")
        tools.s3.put_object.assert_not_called()


# ---------------------------------------------------------------------------
# create_page
# ---------------------------------------------------------------------------


class TestCreatePage:
    def test_own_site_calls_s3(self):
        tools = _make_tools("alice")
        tools.create_page(site="alice", page_path="new-page")
        assert tools.s3.put_object.call_count >= 1

    def test_cross_site_raises_before_s3(self):
        tools = _make_tools("alice")
        with pytest.raises(PermissionError):
            tools.create_page(site="bob", page_path="new-page")
        tools.s3.put_object.assert_not_called()
