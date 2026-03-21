"""Unit tests for API token authentication and rate-limit key function (YOL-27, YOL-31)."""

import base64
import hashlib
import json
import sys
import os
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jwt(sub: str = "user-uuid-123") -> str:
    """Return a minimal (unsigned) JWT with the given sub claim."""
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.sig"


def _make_request(authorization: str | None = None, forwarded_for: str | None = None):
    """Build a minimal mock starlette Request with the given headers."""
    headers: dict[str, str] = {}
    if authorization is not None:
        headers["authorization"] = authorization
    if forwarded_for is not None:
        headers["x-forwarded-for"] = forwarded_for
    req = MagicMock()
    req.headers = headers
    req.client = MagicMock()
    req.client.host = "1.2.3.4"
    return req


# ---------------------------------------------------------------------------
# Rate-limit key function
# ---------------------------------------------------------------------------


class TestRateLimitKey:
    def test_api_token_returns_token_prefix(self):
        from rate_limit import _rate_limit_key

        raw = "as_" + "a" * 64
        req = _make_request(authorization=f"Bearer {raw}")
        key = _rate_limit_key(req)
        assert key.startswith("token:")

    def test_api_token_key_is_sha256_of_raw(self):
        from rate_limit import _rate_limit_key

        raw = "as_" + "b" * 64
        req = _make_request(authorization=f"Bearer {raw}")
        key = _rate_limit_key(req)
        expected = "token:" + hashlib.sha256(raw.encode()).hexdigest()
        assert key == expected

    def test_jwt_token_returns_user_prefix(self):
        from rate_limit import _rate_limit_key

        jwt = _make_jwt("abc-def")
        req = _make_request(authorization=f"Bearer {jwt}")
        key = _rate_limit_key(req)
        assert key == "user:abc-def"

    def test_no_auth_uses_forwarded_for(self):
        from rate_limit import _rate_limit_key

        req = _make_request(forwarded_for="9.8.7.6, 1.1.1.1")
        key = _rate_limit_key(req)
        assert key == "ip:9.8.7.6"

    def test_no_auth_no_forwarded_for_uses_client_ip(self):
        from rate_limit import _rate_limit_key

        req = _make_request()
        key = _rate_limit_key(req)
        assert key == "ip:1.2.3.4"

    def test_malformed_jwt_falls_back_to_ip(self):
        from rate_limit import _rate_limit_key

        req = _make_request(authorization="Bearer notajwt", forwarded_for="5.5.5.5")
        key = _rate_limit_key(req)
        assert key == "ip:5.5.5.5"

    def test_different_api_tokens_get_different_keys(self):
        from rate_limit import _rate_limit_key

        req_a = _make_request(authorization="Bearer as_" + "a" * 64)
        req_b = _make_request(authorization="Bearer as_" + "b" * 64)
        assert _rate_limit_key(req_a) != _rate_limit_key(req_b)


# ---------------------------------------------------------------------------
# resolve_api_token
# ---------------------------------------------------------------------------


class TestResolveApiToken:
    def test_unknown_token_raises_401(self, monkeypatch):
        import auth
        monkeypatch.setattr(auth, "supabase_get_api_token_by_hash", lambda h: None)
        monkeypatch.setattr(auth, "supabase_update_token_last_used", lambda _: None)

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            auth.resolve_api_token("as_" + "x" * 64)
        assert exc_info.value.status_code == 401

    def test_valid_token_returns_user_and_site(self, monkeypatch):
        import auth
        monkeypatch.setattr(auth, "supabase_get_api_token_by_hash", lambda h: {
            "id": "tok-uuid",
            "user_id": "user-abc",
            "site_name": "my-site",
            "expires_at": None,
        })
        last_used_calls = []
        monkeypatch.setattr(auth, "supabase_update_token_last_used", last_used_calls.append)

        user_id, site_name = auth.resolve_api_token("as_" + "a" * 64)
        assert user_id == "user-abc"
        assert site_name == "my-site"
        assert last_used_calls == ["tok-uuid"]

    def test_expired_token_raises_401(self, monkeypatch):
        import auth
        monkeypatch.setattr(auth, "supabase_get_api_token_by_hash", lambda h: {
            "id": "tok-uuid",
            "user_id": "user-abc",
            "site_name": "my-site",
            "expires_at": "2020-01-01T00:00:00Z",  # In the past
        })
        monkeypatch.setattr(auth, "supabase_update_token_last_used", lambda _: None)

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            auth.resolve_api_token("as_" + "a" * 64)
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail

    def test_non_expiring_token_succeeds(self, monkeypatch):
        import auth
        monkeypatch.setattr(auth, "supabase_get_api_token_by_hash", lambda h: {
            "id": "tok-uuid",
            "user_id": "user-abc",
            "site_name": "my-site",
            "expires_at": None,
        })
        monkeypatch.setattr(auth, "supabase_update_token_last_used", lambda _: None)

        user_id, _ = auth.resolve_api_token("as_" + "a" * 64)
        assert user_id == "user-abc"

    def test_future_expiry_token_succeeds(self, monkeypatch):
        import auth
        monkeypatch.setattr(auth, "supabase_get_api_token_by_hash", lambda h: {
            "id": "tok-uuid",
            "user_id": "user-abc",
            "site_name": "my-site",
            "expires_at": "2099-12-31T23:59:59Z",
        })
        monkeypatch.setattr(auth, "supabase_update_token_last_used", lambda _: None)

        user_id, _ = auth.resolve_api_token("as_" + "a" * 64)
        assert user_id == "user-abc"

    def test_hash_passed_to_lookup(self, monkeypatch):
        import auth
        received_hash = {}

        def fake_lookup(h):
            received_hash["hash"] = h
            return None

        monkeypatch.setattr(auth, "supabase_get_api_token_by_hash", fake_lookup)
        monkeypatch.setattr(auth, "supabase_update_token_last_used", lambda _: None)

        raw = "as_" + "c" * 64
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            auth.resolve_api_token(raw)

        expected_hash = hashlib.sha256(raw.encode()).hexdigest()
        assert received_hash["hash"] == expected_hash


# ---------------------------------------------------------------------------
# get_user_context — token path vs JWT path
# ---------------------------------------------------------------------------


class TestGetUserContext:
    def _make_credentials(self, token: str):
        creds = MagicMock()
        creds.credentials = token
        return creds

    def test_as_prefix_routes_to_resolve_api_token(self, monkeypatch):
        import auth
        monkeypatch.setattr(auth, "resolve_api_token", lambda t: ("uid", "site"))
        monkeypatch.setattr(auth, "decode_jwt", lambda c: (_ for _ in ()).throw(AssertionError("should not call decode_jwt")))

        creds = self._make_credentials("as_" + "a" * 64)
        user_id, site = auth.get_user_context(creds)
        assert user_id == "uid"
        assert site == "site"

    def test_jwt_path_calls_decode_jwt(self, monkeypatch):
        import auth
        import dataclasses

        @dataclasses.dataclass
        class FakeClaims:
            user_id: str
            email: str | None

        monkeypatch.setattr(auth, "decode_jwt", lambda c: FakeClaims(user_id="jwt-user", email=None))
        monkeypatch.setattr(auth, "get_site_for_user", lambda uid: "jwt-site")

        creds = self._make_credentials("eyJhbGciOiJSUzI1NiJ9.e30.sig")
        user_id, site = auth.get_user_context(creds)
        assert user_id == "jwt-user"
        assert site == "jwt-site"

    def test_missing_credentials_raises_401(self):
        import auth
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            auth.get_user_context(None)
        assert exc_info.value.status_code == 401
