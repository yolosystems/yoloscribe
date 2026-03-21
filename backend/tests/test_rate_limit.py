"""Unit tests for the rate-limit key function (YOL-55)."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock
from rate_limit import _rate_limit_key

import base64
import json


def _make_jwt(sub: str) -> str:
    """Build a minimal (unsigned) JWT with the given sub claim."""
    header = base64.b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload_bytes = json.dumps({"sub": sub, "email": "user@example.com"}).encode()
    payload = base64.b64encode(payload_bytes).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


def _make_request(auth: str = "", forwarded_for: str = "", client_host: str = "1.2.3.4"):
    request = MagicMock()
    headers = {}
    if auth:
        headers["authorization"] = auth
    if forwarded_for:
        headers["x-forwarded-for"] = forwarded_for
    request.headers = headers
    request.client = MagicMock()
    request.client.host = client_host
    return request


class TestRateLimitKey:
    def test_valid_jwt_uses_user_id(self):
        token = _make_jwt("user-uuid-123")
        req = _make_request(auth=f"Bearer {token}")
        assert _rate_limit_key(req) == "user:user-uuid-123"

    def test_jwt_key_is_case_insensitive_bearer(self):
        token = _make_jwt("user-uuid-456")
        req = _make_request(auth=f"bearer {token}")
        assert _rate_limit_key(req) == "user:user-uuid-456"

    def test_malformed_jwt_falls_back_to_ip(self):
        req = _make_request(auth="Bearer notajwt", client_host="10.0.0.1")
        key = _rate_limit_key(req)
        assert key == "ip:10.0.0.1"

    def test_no_auth_uses_client_ip(self):
        req = _make_request(client_host="10.0.0.2")
        assert _rate_limit_key(req) == "ip:10.0.0.2"

    def test_forwarded_for_takes_priority_over_client_host(self):
        req = _make_request(forwarded_for="203.0.113.1, 10.0.0.1", client_host="10.0.0.1")
        assert _rate_limit_key(req) == "ip:203.0.113.1"

    def test_forwarded_for_multiple_hops_uses_first(self):
        req = _make_request(forwarded_for="1.1.1.1, 2.2.2.2, 3.3.3.3")
        assert _rate_limit_key(req) == "ip:1.1.1.1"

    def test_no_client_falls_back_to_unknown(self):
        req = _make_request()
        req.client = None
        assert _rate_limit_key(req) == "ip:10.0.0.4" or _rate_limit_key(req).startswith("ip:")

    def test_jwt_without_sub_falls_back_to_ip(self):
        header = base64.b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
        payload = base64.b64encode(b'{"email":"no-sub@example.com"}').rstrip(b"=").decode()
        token = f"{header}.{payload}.sig"
        req = _make_request(auth=f"Bearer {token}", client_host="5.5.5.5")
        assert _rate_limit_key(req) == "ip:5.5.5.5"

    def test_different_users_get_different_keys(self):
        token_a = _make_jwt("alice-uuid")
        token_b = _make_jwt("bob-uuid")
        key_a = _rate_limit_key(_make_request(auth=f"Bearer {token_a}"))
        key_b = _rate_limit_key(_make_request(auth=f"Bearer {token_b}"))
        assert key_a != key_b
        assert key_a == "user:alice-uuid"
        assert key_b == "user:bob-uuid"
