"""Unit tests for the messaging-config repositories (Item 3).

SupabaseMessagingConfigRepository must format the exact PostgREST calls
messaging.py previously issued inline (behavior-preserving refactor). The
DynamoDB unmarshaller must parse the stored connection JSON.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class TestSupabaseMessagingConfigRepository:
    def _repo(self):
        from auth_providers.supabase import SupabaseMessagingConfigRepository
        return SupabaseMessagingConfigRepository("https://fake.supabase.co", "fake-key")

    def test_list_by_token_ids_builds_in_filter(self, monkeypatch):
        import urllib.request

        repo = self._repo()
        captured = {}

        def fake_urlopen(req):
            captured["url"] = req.full_url
            return _FakeResp(b'[{"id":"c1","platform":"discord","connection":{},"created_at":"t","api_token_id":"t1"}]')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        rows = repo.list_by_token_ids(["t1", "t2"])

        assert len(rows) == 1
        assert "messaging_configs" in captured["url"]
        assert "api_token_id=in." in captured["url"]
        assert "t1" in captured["url"] and "t2" in captured["url"]

    def test_list_by_token_ids_empty_short_circuits(self, monkeypatch):
        import urllib.request

        repo = self._repo()

        def fake_urlopen(req):  # pragma: no cover - must not be called
            raise AssertionError("network should not be touched for empty token_ids")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert repo.list_by_token_ids([]) == []

    def test_get_returns_first_row_or_none(self, monkeypatch):
        import urllib.request

        repo = self._repo()

        def fake_urlopen(req):
            return _FakeResp(b'[{"id":"c1","api_token_id":"t1"}]')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        row = repo.get("c1")
        assert row == {"id": "c1", "api_token_id": "t1"}

    def test_get_missing_returns_none(self, monkeypatch):
        import urllib.request

        repo = self._repo()
        monkeypatch.setattr(urllib.request, "urlopen", lambda req: _FakeResp(b"[]"))
        assert repo.get("nope") is None

    def test_delete_uses_delete_method(self, monkeypatch):
        import urllib.request

        repo = self._repo()
        captured = {}

        def fake_urlopen(req):
            captured["url"] = req.full_url
            captured["method"] = req.method
            return _FakeResp(b"")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        repo.delete("c1")
        assert captured["method"] == "DELETE"
        assert "id=eq.c1" in captured["url"]


class TestDynamoDBUnmarshalMessagingConfig:
    def test_parses_connection_json_string(self):
        from auth_providers.dynamodb import _unmarshal_messaging_config

        item = {
            "id": {"S": "c1"},
            "platform": {"S": "slack"},
            "connection": {"S": '{"channel_id": "C123"}'},
            "created_at": {"S": "2026-01-01"},
            "api_token_id": {"S": "t1"},
        }
        row = _unmarshal_messaging_config(item)
        assert row == {
            "id": "c1",
            "platform": "slack",
            "connection": {"channel_id": "C123"},
            "created_at": "2026-01-01",
            "api_token_id": "t1",
        }

    def test_malformed_connection_defaults_to_empty(self):
        from auth_providers.dynamodb import _unmarshal_messaging_config

        row = _unmarshal_messaging_config({"id": {"S": "c1"}, "connection": {"S": "not-json"}})
        assert row["connection"] == {}
        assert row["id"] == "c1"
