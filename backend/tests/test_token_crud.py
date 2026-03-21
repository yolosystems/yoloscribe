"""Unit tests for API token generation and management (YOL-21, YOL-22, YOL-29)."""

import hashlib
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from routers.tokens import _generate_token, _hash_token, _TOKEN_PREFIX, _TOKEN_BYTES


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------


class TestTokenGeneration:
    def test_token_has_correct_prefix(self):
        token = _generate_token()
        assert token.startswith(_TOKEN_PREFIX)

    def test_token_hex_part_has_correct_length(self):
        token = _generate_token()
        hex_part = token[len(_TOKEN_PREFIX):]
        assert len(hex_part) == _TOKEN_BYTES * 2  # 32 bytes → 64 hex chars

    def test_token_hex_part_is_valid_hex(self):
        token = _generate_token()
        hex_part = token[len(_TOKEN_PREFIX):]
        int(hex_part, 16)  # raises ValueError if not valid hex

    def test_tokens_are_unique(self):
        tokens = {_generate_token() for _ in range(100)}
        assert len(tokens) == 100


# ---------------------------------------------------------------------------
# Token hashing
# ---------------------------------------------------------------------------


class TestTokenHashing:
    def test_hash_is_sha256_hex(self):
        raw = "as_abc123"
        result = _hash_token(raw)
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert result == expected

    def test_hash_length_is_64(self):
        assert len(_hash_token(_generate_token())) == 64

    def test_hash_is_deterministic(self):
        raw = _generate_token()
        assert _hash_token(raw) == _hash_token(raw)

    def test_different_tokens_have_different_hashes(self):
        a, b = _generate_token(), _generate_token()
        assert _hash_token(a) != _hash_token(b)

    def test_raw_token_not_recoverable_from_hash(self):
        raw = _generate_token()
        hashed = _hash_token(raw)
        # Hash must not contain the original token value
        assert raw not in hashed
        assert _TOKEN_PREFIX not in hashed


# ---------------------------------------------------------------------------
# Supabase helper stubs (unit-level, no network calls)
# ---------------------------------------------------------------------------


class TestSupabaseTokenHelpers:
    """Verify that helpers correctly format PostgREST calls without hitting Supabase."""

    def test_insert_builds_correct_payload(self, monkeypatch):
        import supabase_helpers as sh
        calls = []

        class _FakeResp:
            def read(self):
                return b'[{"id": "test-uuid"}]'
            def __enter__(self): return self
            def __exit__(self, *a): pass

        monkeypatch.setattr(sh, "SUPABASE_URL", "https://fake.supabase.co")
        monkeypatch.setattr(sh, "SUPABASE_SERVICE_ROLE_KEY", "fake-key")

        import urllib.request
        import json

        captured = {}
        original_urlopen = urllib.request.urlopen

        def fake_urlopen(req):
            captured["url"] = req.full_url
            captured["data"] = json.loads(req.data)
            captured["method"] = req.method
            return _FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = sh.supabase_insert_api_token("uid", "mysite", "My Bot", "hash123")

        assert result == "test-uuid"
        assert "api_tokens" in captured["url"]
        assert captured["data"]["user_id"] == "uid"
        assert captured["data"]["site_name"] == "mysite"
        assert captured["data"]["name"] == "My Bot"
        assert captured["data"]["token_hash"] == "hash123"
        assert captured["method"] == "POST"

    def test_list_filters_revoked(self, monkeypatch):
        import supabase_helpers as sh
        monkeypatch.setattr(sh, "SUPABASE_URL", "https://fake.supabase.co")
        monkeypatch.setattr(sh, "SUPABASE_SERVICE_ROLE_KEY", "fake-key")

        import urllib.request

        class _FakeResp:
            def read(self):
                return b'[{"id": "a"}, {"id": "b"}]'
            def __enter__(self): return self
            def __exit__(self, *a): pass

        captured_url = {}

        def fake_urlopen(req):
            captured_url["url"] = req.full_url
            return _FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        rows = sh.supabase_list_api_tokens("uid")

        assert len(rows) == 2
        assert "revoked_at=is.null" in captured_url["url"]

    def test_revoke_patches_revoked_at(self, monkeypatch):
        import supabase_helpers as sh
        monkeypatch.setattr(sh, "SUPABASE_URL", "https://fake.supabase.co")
        monkeypatch.setattr(sh, "SUPABASE_SERVICE_ROLE_KEY", "fake-key")

        import urllib.request, json

        class _FakeResp:
            def read(self): return b'[{"id": "tok"}]'
            def __enter__(self): return self
            def __exit__(self, *a): pass

        captured = {}

        def fake_urlopen(req):
            captured["url"] = req.full_url
            captured["data"] = json.loads(req.data)
            captured["method"] = req.method
            return _FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        found = sh.supabase_revoke_api_token("tok-id", "uid")

        assert found is True
        assert captured["method"] == "PATCH"
        assert "revoked_at" in captured["data"]
        assert "tok-id" in captured["url"]
        assert "uid" in captured["url"]
