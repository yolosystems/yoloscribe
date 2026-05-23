import json
import pytest

from yoloscribe_io.events import EventType
from yoloscribe_io.secrets import LocalSecretStore
from yoloscribe_io.webhooks import (
    APIToken,
    APITokenData,
    WebhookEntry,
    Webhooks,
)


# ── helpers ───────────────────────────────────────────────────────────────────

class CapturingHandler:
    def __init__(self):
        self.events = []

    def handle(self, event):
        self.events.append(event)


@pytest.fixture
def store():
    return LocalSecretStore()


# ── WebhookEntry ──────────────────────────────────────────────────────────────

def test_webhook_entry_from_dict():
    e = WebhookEntry.from_dict({"label": "Slack", "url": "https://hooks.slack.com/x"})
    assert e.label == "Slack"
    assert e.url == "https://hooks.slack.com/x"


def test_webhook_entry_from_dict_defaults():
    e = WebhookEntry.from_dict({})
    assert e.label == ""
    assert e.url == ""


def test_webhook_entry_to_dict_roundtrip():
    e = WebhookEntry(label="Discord", url="https://discord.com/api/webhooks/x")
    assert WebhookEntry.from_dict(e.to_dict()) == e


# ── Webhooks — key ────────────────────────────────────────────────────────────

def test_webhooks_key(store):
    w = Webhooks("user-1", store)
    assert w._key == "yoloscribe/user-1/webhooks"


# ── Webhooks — list ───────────────────────────────────────────────────────────

def test_webhooks_list_empty(store):
    w = Webhooks("u", store)
    assert w.list() == []


def test_webhooks_list_returns_entries(store):
    data = json.dumps([{"label": "Slack", "url": "https://s.example.com"}])
    store.put("yoloscribe/u/webhooks", data)
    w = Webhooks("u", store)
    entries = w.list()
    assert len(entries) == 1
    assert entries[0].label == "Slack"


def test_webhooks_list_malformed_json_returns_empty(store):
    store.put("yoloscribe/u/webhooks", "not json {")
    w = Webhooks("u", store)
    assert w.list() == []


def test_webhooks_list_skips_non_dict_items(store):
    store.put("yoloscribe/u/webhooks", json.dumps([{"label": "A", "url": "x"}, "bad"]))
    w = Webhooks("u", store)
    assert len(w.list()) == 1


# ── Webhooks — add ────────────────────────────────────────────────────────────

def test_webhooks_add_persists(store):
    w = Webhooks("u", store)
    w.add("Slack", "https://s.example.com")
    assert len(w.list()) == 1
    assert w.list()[0].label == "Slack"


def test_webhooks_add_multiple(store):
    w = Webhooks("u", store)
    w.add("A", "https://a.example.com")
    w.add("B", "https://b.example.com")
    assert len(w.list()) == 2


def test_webhooks_add_emits_webhook_added(store):
    w = Webhooks("u", store)
    cap = CapturingHandler()
    w.add_handler(cap)
    w.add("Slack", "https://s.example.com")
    assert cap.events[0].type == EventType.WEBHOOK_ADDED


def test_webhooks_add_event_payload(store):
    w = Webhooks("u", store)
    cap = CapturingHandler()
    w.add_handler(cap)
    w.add("Slack", "https://s.example.com")
    ev = cap.events[0]
    assert ev.payload["label"] == "Slack"
    assert ev.payload["url"] == "https://s.example.com"
    assert ev.payload["user_id"] == "u"


def test_webhooks_add_appends_to_existing(store):
    w = Webhooks("u", store)
    w.add("First", "https://a.example.com")
    w.add("Second", "https://b.example.com")
    labels = [e.label for e in w.list()]
    assert "First" in labels
    assert "Second" in labels


# ── Webhooks — remove ─────────────────────────────────────────────────────────

def test_webhooks_remove_by_label(store):
    w = Webhooks("u", store)
    w.add("Slack", "https://s.example.com")
    result = w.remove("Slack")
    assert result is True
    assert w.list() == []


def test_webhooks_remove_returns_false_when_not_found(store):
    w = Webhooks("u", store)
    assert w.remove("nonexistent") is False


def test_webhooks_remove_emits_webhook_removed(store):
    w = Webhooks("u", store)
    w.add("Slack", "https://s.example.com")
    cap = CapturingHandler()
    w.add_handler(cap)
    w.remove("Slack")
    assert cap.events[0].type == EventType.WEBHOOK_REMOVED


def test_webhooks_remove_event_payload(store):
    w = Webhooks("u", store)
    w.add("Slack", "https://s.example.com")
    cap = CapturingHandler()
    w.add_handler(cap)
    w.remove("Slack")
    ev = cap.events[0]
    assert ev.payload["label"] == "Slack"
    assert ev.payload["user_id"] == "u"


def test_webhooks_remove_only_first_matching_label(store):
    w = Webhooks("u", store)
    w.add("Dup", "https://a.example.com")
    w.add("Dup", "https://b.example.com")
    w.remove("Dup")
    assert len(w.list()) == 1


def test_webhooks_remove_no_event_when_not_found(store):
    w = Webhooks("u", store)
    cap = CapturingHandler()
    w.add_handler(cap)
    w.remove("nonexistent")
    assert cap.events == []


# ── Webhooks — remove_by_url ──────────────────────────────────────────────────

def test_webhooks_remove_by_url(store):
    w = Webhooks("u", store)
    w.add("Slack", "https://s.example.com")
    result = w.remove_by_url("https://s.example.com")
    assert result is True
    assert w.list() == []


def test_webhooks_remove_by_url_returns_false_when_not_found(store):
    w = Webhooks("u", store)
    assert w.remove_by_url("https://nonexistent.com") is False


def test_webhooks_remove_by_url_emits_webhook_removed(store):
    w = Webhooks("u", store)
    w.add("Slack", "https://s.example.com")
    cap = CapturingHandler()
    w.add_handler(cap)
    w.remove_by_url("https://s.example.com")
    assert cap.events[0].type == EventType.WEBHOOK_REMOVED


def test_webhooks_remove_by_url_payload_includes_url(store):
    w = Webhooks("u", store)
    w.add("Slack", "https://s.example.com")
    cap = CapturingHandler()
    w.add_handler(cap)
    w.remove_by_url("https://s.example.com")
    assert cap.events[0].payload["url"] == "https://s.example.com"
    assert cap.events[0].payload["label"] == "Slack"


# ── APITokenData ──────────────────────────────────────────────────────────────

def test_api_token_data_from_dict():
    td = APITokenData.from_dict({"token_hash": "abc", "site": "mysite", "expires_at": 9999})
    assert td.token_hash == "abc"
    assert td.site == "mysite"
    assert td.expires_at == 9999


def test_api_token_data_defaults():
    td = APITokenData.from_dict({})
    assert td.token_hash == ""
    assert td.site == ""
    assert td.expires_at == 0


def test_api_token_data_to_dict_roundtrip():
    td = APITokenData(token_hash="h", site="s", expires_at=100)
    assert APITokenData.from_dict(td.to_dict()) == td


# ── APIToken — key ────────────────────────────────────────────────────────────

def test_api_token_key(store):
    t = APIToken("user-1", store)
    assert t._key == "yoloscribe/user-1/api_token"


# ── APIToken — load ───────────────────────────────────────────────────────────

def test_api_token_load_returns_none_when_absent(store):
    t = APIToken("u", store)
    assert t.load() is None


def test_api_token_load_returns_none_on_malformed_json(store):
    store.put("yoloscribe/u/api_token", "bad json {")
    t = APIToken("u", store)
    assert t.load() is None


# ── APIToken — create ─────────────────────────────────────────────────────────

def test_api_token_create_persists(store):
    t = APIToken("u", store)
    t.create(APITokenData(token_hash="h", site="mysite", expires_at=9999))
    loaded = t.load()
    assert loaded is not None
    assert loaded.token_hash == "h"
    assert loaded.site == "mysite"


def test_api_token_create_emits_token_created(store):
    t = APIToken("u", store)
    cap = CapturingHandler()
    t.add_handler(cap)
    t.create(APITokenData(token_hash="h", site="mysite", expires_at=9999))
    assert cap.events[0].type == EventType.TOKEN_CREATED


def test_api_token_create_event_payload(store):
    t = APIToken("u", store)
    cap = CapturingHandler()
    t.add_handler(cap)
    t.create(APITokenData(token_hash="h", site="mysite", expires_at=9999))
    ev = cap.events[0]
    assert ev.payload["user_id"] == "u"
    assert ev.payload["site"] == "mysite"


# ── APIToken — revoke ─────────────────────────────────────────────────────────

def test_api_token_revoke_deletes_secret(store):
    t = APIToken("u", store)
    t.create(APITokenData(token_hash="h", site="s", expires_at=0))
    t.revoke()
    assert not t.exists()


def test_api_token_revoke_emits_token_revoked(store):
    t = APIToken("u", store)
    t.create(APITokenData(token_hash="h", site="s", expires_at=0))
    cap = CapturingHandler()
    t.add_handler(cap)
    t.revoke()
    assert cap.events[0].type == EventType.TOKEN_REVOKED


def test_api_token_revoke_event_payload(store):
    t = APIToken("u", store)
    t.create(APITokenData(token_hash="h", site="s", expires_at=0))
    cap = CapturingHandler()
    t.add_handler(cap)
    t.revoke()
    assert cap.events[0].payload["user_id"] == "u"
