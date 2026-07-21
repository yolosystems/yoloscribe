"""Unit tests for _MCPAuthMiddleware's three-way auth dispatch: local static key,
run token, and full user JWT (Supabase/Cognito) — added when run-token support
was introduced alongside the existing two paths.
"""

import asyncio
import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import run_tokens
from mcp_server import _MCPAuthMiddleware


class _Store:
    def __init__(self):
        self._data = {}

    def get(self, key):
        return self._data.get(key)

    def put(self, key, value, description=""):
        self._data[key] = value

    def exists(self, key):
        return key in self._data

    def delete(self, key):
        self._data.pop(key, None)


def _make_request(authorization: str | None = None):
    req = MagicMock()
    req.headers = {"authorization": authorization} if authorization is not None else {}
    req.method = "POST"
    req.state = MagicMock()
    return req


async def _noop_call_next(request):
    resp = MagicMock()
    resp.status_code = 200
    return resp


def _run(coro):
    return asyncio.run(coro)


class TestRunTokenDispatch:
    def setup_method(self):
        run_tokens._private_key_pem = None
        run_tokens._public_key_pem = None
        run_tokens._kid = ""
        run_tokens.load_signing_key(_Store(), local_mode=True)

    def teardown_method(self):
        run_tokens._private_key_pem = None
        run_tokens._public_key_pem = None
        run_tokens._kid = ""

    def _middleware(self, local_mode=False):
        return _MCPAuthMiddleware(
            app=MagicMock(),
            auth_provider=MagicMock(),
            user_site_repo=MagicMock(),
            local_mode=local_mode,
            local_site_name="local",
            local_user_id="local-user-00000000",
            local_api_key="local",
        )

    def test_run_token_authenticates_non_local(self):
        token = run_tokens.mint_run_token(
            site="alice-site", user_id="user-1", agent_name="tidy-bot",
            agent_type="page", page_path="features/auth",
        )
        mw = self._middleware(local_mode=False)
        request = _make_request(authorization=f"Bearer {token}")
        _run(mw.dispatch(request, _noop_call_next))

        user = request.state.mcp_user
        assert user.site == "alice-site"
        assert user.user_id == "user-1"
        assert user.agent_name == "tidy-bot"
        assert user.path_scope == [run_tokens.PathScopeEntry("features/auth", ["read", "write-content"])]
        # Full-JWT path must not have been consulted for a run token.
        mw._auth_provider.decode_jwt.assert_not_called()

    def test_run_token_authenticates_in_local_mode_too(self):
        # A run token must work in LOCAL_MODE even though it isn't the static
        # local API key — LOCAL_RUNNER-driven agent-runner jobs need this path.
        token = run_tokens.mint_run_token(site="s", user_id="u", agent_name="a", agent_type="ingest")
        mw = self._middleware(local_mode=True)
        request = _make_request(authorization=f"Bearer {token}")
        _run(mw.dispatch(request, _noop_call_next))
        assert request.state.mcp_user.site == "s"
        assert request.state.mcp_user.path_scope == [run_tokens.PathScopeEntry("", ["read", "write-content"])]

    def test_expired_run_token_rejected(self):
        token = run_tokens.mint_run_token(site="s", user_id="u", agent_name="a", agent_type="page", ttl_seconds=-5)
        mw = self._middleware(local_mode=False)
        request = _make_request(authorization=f"Bearer {token}")
        response = _run(mw.dispatch(request, _noop_call_next))
        assert response.status_code == 401

    def test_local_static_key_still_works(self):
        mw = self._middleware(local_mode=True)
        request = _make_request(authorization="Bearer local")
        _run(mw.dispatch(request, _noop_call_next))
        assert request.state.mcp_user.user_id == "local-user-00000000"
        assert request.state.mcp_user.path_scope is None

    def test_full_jwt_path_unaffected(self):
        mw = self._middleware(local_mode=False)

        class _Claims:
            user_id = "user-42"
            email = "u@example.com"

        mw._auth_provider.decode_jwt.return_value = _Claims()
        mw._user_site_repo.get_site_for_user.return_value = "bobs-site"

        request = _make_request(authorization="Bearer some.supabase.jwt")
        _run(mw.dispatch(request, _noop_call_next))

        user = request.state.mcp_user
        assert user.user_id == "user-42"
        assert user.site == "bobs-site"
        assert user.path_scope is None  # unrestricted, exactly as before run tokens existed

    def test_local_mode_rejects_non_matching_non_run_token(self):
        mw = self._middleware(local_mode=True)
        request = _make_request(authorization="Bearer garbage")
        response = _run(mw.dispatch(request, _noop_call_next))
        assert response.status_code == 401

    def test_missing_header_rejected(self):
        mw = self._middleware(local_mode=False)
        request = _make_request(authorization=None)
        response = _run(mw.dispatch(request, _noop_call_next))
        assert response.status_code == 401
