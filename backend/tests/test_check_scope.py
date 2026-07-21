"""Unit tests for _check_scope — the path_scope enforcement matrix for run-token callers."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import HTTPException

import run_tokens
from mcp_server import _MCPUser, _check_scope


def _page_user(page_path: str = "features/auth") -> _MCPUser:
    return _MCPUser(
        user_id="u", email=None, site="s",
        path_scope=[run_tokens.PathScopeEntry(page_path, ["read", "write-content"])],
        run_id="r1", agent_name="page-agent",
    )


def _ingest_user() -> _MCPUser:
    return _MCPUser(
        user_id="u", email=None, site="s",
        path_scope=[run_tokens.PathScopeEntry("", ["read", "write-content"])],
        run_id="r2", agent_name="ingest-agent",
    )


def _notification_user() -> _MCPUser:
    return _MCPUser(
        user_id="u", email=None, site="s",
        path_scope=[run_tokens.PathScopeEntry("", ["read", "notify"])],
        run_id="r3", agent_name="notify-agent",
    )


def _full_jwt_user() -> _MCPUser:
    return _MCPUser(user_id="u", email="u@example.com", site="s")  # path_scope=None


class TestFullJwtUnrestricted:
    """A full user JWT (site owner) is never restricted — today's behavior, unchanged."""

    @pytest.mark.parametrize("op", ["read", "write-content", "write-settings", "write-agent", "delete", "notify"])
    def test_every_operation_allowed_everywhere(self, op):
        _check_scope(_full_jwt_user(), "anything/at/all", op)  # must not raise


class TestPageToken:
    def test_read_own_page_allowed(self):
        _check_scope(_page_user("features/auth"), "features/auth", "read")

    def test_write_content_own_page_allowed(self):
        _check_scope(_page_user("features/auth"), "features/auth", "write-content")

    def test_read_other_page_denied(self):
        with pytest.raises(HTTPException) as exc_info:
            _check_scope(_page_user("features/auth"), "features/billing", "read")
        assert exc_info.value.status_code == 403

    def test_write_other_page_denied(self):
        with pytest.raises(HTTPException):
            _check_scope(_page_user("features/auth"), "features/billing", "write-content")

    def test_delete_own_page_denied(self):
        with pytest.raises(HTTPException):
            _check_scope(_page_user("features/auth"), "features/auth", "delete")

    def test_notify_denied(self):
        with pytest.raises(HTTPException):
            _check_scope(_page_user("features/auth"), "features/auth", "notify")

    def test_sibling_path_prefix_collision_denied(self):
        # "features/auth-v2" must not be treated as within scope of "features/auth".
        with pytest.raises(HTTPException):
            _check_scope(_page_user("features/auth"), "features/auth-v2", "read")

    def test_child_path_of_own_page_allowed(self):
        # A page's own subtree (if ever addressed) is in scope via the "prefix + /" check.
        _check_scope(_page_user("features/auth"), "features/auth/sub", "read")


class TestIngestToken:
    @pytest.mark.parametrize("page_path", ["", "features/auth", "any/arbitrary/destination"])
    def test_read_anywhere_allowed(self, page_path):
        _check_scope(_ingest_user(), page_path, "read")

    @pytest.mark.parametrize("page_path", ["", "features/auth", "any/arbitrary/destination"])
    def test_write_content_anywhere_allowed(self, page_path):
        _check_scope(_ingest_user(), page_path, "write-content")

    def test_delete_denied(self):
        with pytest.raises(HTTPException):
            _check_scope(_ingest_user(), "features/auth", "delete")

    def test_write_agent_denied(self):
        with pytest.raises(HTTPException):
            _check_scope(_ingest_user(), "", "write-agent")

    def test_write_settings_denied(self):
        with pytest.raises(HTTPException):
            _check_scope(_ingest_user(), "features/auth", "write-settings")

    def test_notify_denied(self):
        with pytest.raises(HTTPException):
            _check_scope(_ingest_user(), "", "notify")


class TestNotificationToken:
    def test_read_allowed(self):
        _check_scope(_notification_user(), "", "read")
        _check_scope(_notification_user(), "features/auth", "read")

    def test_notify_allowed(self):
        _check_scope(_notification_user(), "", "notify")

    def test_write_content_denied(self):
        with pytest.raises(HTTPException):
            _check_scope(_notification_user(), "", "write-content")

    def test_write_agent_denied(self):
        with pytest.raises(HTTPException):
            _check_scope(_notification_user(), "", "write-agent")

    def test_delete_denied(self):
        with pytest.raises(HTTPException):
            _check_scope(_notification_user(), "", "delete")


class TestErrorMessage:
    def test_denial_names_agent_and_operation(self):
        with pytest.raises(HTTPException) as exc_info:
            _check_scope(_page_user("features/auth"), "features/billing", "write-content")
        detail = exc_info.value.detail
        assert "page-agent" in detail
        assert "write-content" in detail
        assert "features/billing" in detail
