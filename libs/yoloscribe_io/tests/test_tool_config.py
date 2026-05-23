import json
import pytest

from yoloscribe_io.events import EventType
from yoloscribe_io.secrets import LocalSecretStore
from yoloscribe_io.storage import LocalStorageBackend
from yoloscribe_io.tool_config import (
    OAuthClientConfig,
    TokenData,
    ToolConfig,
    ToolToken,
    list_tools,
    load_tool_config,
)


# ── helpers ───────────────────────────────────────────────────────────────────

_LINEAR_MCP = json.dumps({
    "mcpServers": {
        "linear": {
            "url": "https://mcp.linear.app/mcp",
            "transport": "streamable-http",
            "auth": "oauth",
        }
    }
})

_STDIO_MCP = json.dumps({
    "mcpServers": {
        "notifications": {
            "command": "notification-mcp",
        }
    }
})

_OAUTH_CLIENT = json.dumps({
    "client_id": "abc123",
    "scopes": ["read", "write"],
    "issuer": "https://linear.app",
    "extra_field": "extra_value",
})

_TOKEN_DATA = {
    "access_token": "tok_abc",
    "refresh_token": "ref_xyz",
    "expires_at": 9999999999,
    "client_id": "abc123",
    "scope": "read write",
    "auth_server_metadata": {"token_endpoint": "https://linear.app/oauth/token"},
}


class CapturingHandler:
    def __init__(self):
        self.events = []

    def handle(self, event):
        self.events.append(event)


# ── OAuthClientConfig ─────────────────────────────────────────────────────────

def test_oauth_client_from_dict_basic():
    cfg = OAuthClientConfig.from_dict({"client_id": "x", "scopes": ["a"], "issuer": "https://y"})
    assert cfg.client_id == "x"
    assert cfg.scopes == ["a"]
    assert cfg.issuer == "https://y"


def test_oauth_client_from_dict_extra_fields():
    cfg = OAuthClientConfig.from_dict({"client_id": "x", "unknown_key": "val"})
    assert cfg.extra["unknown_key"] == "val"


def test_oauth_client_defaults_empty():
    cfg = OAuthClientConfig.from_dict({})
    assert cfg.client_id == ""
    assert cfg.scopes == []
    assert cfg.issuer == ""


# ── load_tool_config — missing tool ──────────────────────────────────────────

def test_load_tool_config_missing_returns_none():
    store = LocalStorageBackend()
    assert load_tool_config("linear", store) is None


# ── load_tool_config — remote OAuth tool ─────────────────────────────────────

def test_load_tool_config_http_transport():
    store = LocalStorageBackend({".tools/linear/mcp.json": _LINEAR_MCP})
    cfg = load_tool_config("linear", store)
    assert cfg is not None
    assert cfg.transport == "streamable-http"


def test_load_tool_config_http_url():
    store = LocalStorageBackend({".tools/linear/mcp.json": _LINEAR_MCP})
    cfg = load_tool_config("linear", store)
    assert cfg.url == "https://mcp.linear.app/mcp"


def test_load_tool_config_requires_oauth_true():
    store = LocalStorageBackend({".tools/linear/mcp.json": _LINEAR_MCP})
    cfg = load_tool_config("linear", store)
    assert cfg.requires_oauth is True


def test_load_tool_config_name():
    store = LocalStorageBackend({".tools/linear/mcp.json": _LINEAR_MCP})
    cfg = load_tool_config("linear", store)
    assert cfg.name == "linear"


# ── load_tool_config — stdio tool ─────────────────────────────────────────────

def test_load_tool_config_stdio_command():
    store = LocalStorageBackend({".tools/notifications/mcp.json": _STDIO_MCP})
    cfg = load_tool_config("notifications", store)
    assert cfg.command == "notification-mcp"


def test_load_tool_config_stdio_no_oauth():
    store = LocalStorageBackend({".tools/notifications/mcp.json": _STDIO_MCP})
    cfg = load_tool_config("notifications", store)
    assert cfg.requires_oauth is False
    assert cfg.url == ""


def test_load_tool_config_stdio_no_oauth_client():
    store = LocalStorageBackend({".tools/notifications/mcp.json": _STDIO_MCP})
    cfg = load_tool_config("notifications", store)
    assert cfg.oauth_client is None


# ── load_tool_config — oauth_client.json ─────────────────────────────────────

def test_load_tool_config_with_oauth_client():
    store = LocalStorageBackend({
        ".tools/linear/mcp.json": _LINEAR_MCP,
        ".tools/linear/oauth_client.json": _OAUTH_CLIENT,
    })
    cfg = load_tool_config("linear", store)
    assert cfg.oauth_client is not None
    assert cfg.oauth_client.client_id == "abc123"
    assert cfg.oauth_client.scopes == ["read", "write"]


def test_load_tool_config_oauth_client_extra_fields():
    store = LocalStorageBackend({
        ".tools/linear/mcp.json": _LINEAR_MCP,
        ".tools/linear/oauth_client.json": _OAUTH_CLIENT,
    })
    cfg = load_tool_config("linear", store)
    assert cfg.oauth_client.extra["extra_field"] == "extra_value"


def test_load_tool_config_without_oauth_client():
    store = LocalStorageBackend({".tools/linear/mcp.json": _LINEAR_MCP})
    cfg = load_tool_config("linear", store)
    assert cfg.oauth_client is None


# ── load_tool_config — malformed JSON ────────────────────────────────────────

def test_load_tool_config_malformed_mcp_returns_empty_config():
    store = LocalStorageBackend({".tools/bad/mcp.json": "not json {"})
    cfg = load_tool_config("bad", store)
    assert isinstance(cfg, ToolConfig)
    assert cfg.transport == ""


def test_load_tool_config_malformed_oauth_client_ignored():
    store = LocalStorageBackend({
        ".tools/linear/mcp.json": _LINEAR_MCP,
        ".tools/linear/oauth_client.json": "not json {",
    })
    cfg = load_tool_config("linear", store)
    assert cfg.oauth_client is None


# ── load_tool_config — raw_mcp ────────────────────────────────────────────────

def test_load_tool_config_raw_mcp_preserved():
    store = LocalStorageBackend({".tools/linear/mcp.json": _LINEAR_MCP})
    cfg = load_tool_config("linear", store)
    assert "mcpServers" in cfg.raw_mcp


# ── list_tools ────────────────────────────────────────────────────────────────

def test_list_tools_empty():
    assert list_tools(LocalStorageBackend()) == []


def test_list_tools_finds_tools():
    store = LocalStorageBackend({
        ".tools/linear/mcp.json": _LINEAR_MCP,
        ".tools/github/mcp.json": "{}",
    })
    names = list_tools(store)
    assert "linear" in names
    assert "github" in names


def test_list_tools_deduplicates():
    store = LocalStorageBackend({
        ".tools/linear/mcp.json": _LINEAR_MCP,
        ".tools/linear/oauth_client.json": _OAUTH_CLIENT,
    })
    names = list_tools(store)
    assert names.count("linear") == 1


def test_list_tools_sorted():
    store = LocalStorageBackend({
        ".tools/z-tool/mcp.json": "{}",
        ".tools/a-tool/mcp.json": "{}",
    })
    names = list_tools(store)
    assert names == sorted(names)


# ── TokenData ─────────────────────────────────────────────────────────────────

def test_token_data_from_dict():
    td = TokenData.from_dict(_TOKEN_DATA)
    assert td.access_token == "tok_abc"
    assert td.refresh_token == "ref_xyz"
    assert td.expires_at == 9999999999
    assert td.client_id == "abc123"
    assert td.scope == "read write"


def test_token_data_to_dict_roundtrip():
    td = TokenData.from_dict(_TOKEN_DATA)
    d = td.to_dict()
    assert d["access_token"] == "tok_abc"
    assert d["refresh_token"] == "ref_xyz"
    assert d["expires_at"] == 9999999999


def test_token_data_client_secret_omitted_when_none():
    td = TokenData.from_dict(_TOKEN_DATA)
    assert "client_secret" not in td.to_dict()


def test_token_data_client_secret_included_when_set():
    data = {**_TOKEN_DATA, "client_secret": "secret_val"}
    td = TokenData.from_dict(data)
    assert td.to_dict()["client_secret"] == "secret_val"


def test_token_data_defaults():
    td = TokenData.from_dict({})
    assert td.access_token == ""
    assert td.expires_at == 0


# ── ToolToken — construction ──────────────────────────────────────────────────

def test_tool_token_key():
    t = ToolToken("user-1", "linear", LocalSecretStore())
    assert t._key == "yoloscribe/user-1/oauth/linear"


def test_tool_token_tool_name():
    t = ToolToken("user-1", "linear", LocalSecretStore())
    assert t.tool_name == "linear"


def test_tool_token_user_id():
    t = ToolToken("user-1", "linear", LocalSecretStore())
    assert t.user_id == "user-1"


# ── ToolToken — save / load ───────────────────────────────────────────────────

def test_tool_token_save_and_load():
    store = LocalSecretStore()
    t = ToolToken("user-1", "linear", store)
    td = TokenData.from_dict(_TOKEN_DATA)
    t.save(td)
    loaded = t.load()
    assert loaded is not None
    assert loaded.access_token == "tok_abc"


def test_tool_token_load_returns_none_when_absent():
    t = ToolToken("user-1", "linear", LocalSecretStore())
    assert t.load() is None


def test_tool_token_load_returns_none_on_malformed_json():
    store = LocalSecretStore({"yoloscribe/user-1/oauth/linear": "bad json {"})
    t = ToolToken("user-1", "linear", store)
    assert t.load() is None


# ── ToolToken — events ────────────────────────────────────────────────────────

def test_tool_token_save_emits_auth_started():
    t = ToolToken("u", "linear", LocalSecretStore())
    cap = CapturingHandler()
    t.add_handler(cap)
    t.save(TokenData.from_dict(_TOKEN_DATA))
    types = [e.type for e in cap.events]
    assert EventType.TOOL_AUTH_STARTED in types


def test_tool_token_save_emits_auth_completed():
    t = ToolToken("u", "linear", LocalSecretStore())
    cap = CapturingHandler()
    t.add_handler(cap)
    t.save(TokenData.from_dict(_TOKEN_DATA))
    types = [e.type for e in cap.events]
    assert EventType.TOOL_AUTH_COMPLETED in types


def test_tool_token_save_event_payload_contains_tool_name():
    t = ToolToken("u", "linear", LocalSecretStore())
    cap = CapturingHandler()
    t.add_handler(cap)
    t.save(TokenData.from_dict(_TOKEN_DATA))
    completed = next(e for e in cap.events if e.type == EventType.TOOL_AUTH_COMPLETED)
    assert completed.payload["tool_name"] == "linear"
    assert completed.payload["user_id"] == "u"


def test_tool_token_revoke_emits_auth_revoked():
    store = LocalSecretStore()
    t = ToolToken("u", "linear", store)
    t.save(TokenData.from_dict(_TOKEN_DATA))
    cap = CapturingHandler()
    t.add_handler(cap)
    t.revoke()
    types = [e.type for e in cap.events]
    assert EventType.TOOL_AUTH_REVOKED in types


def test_tool_token_revoke_deletes_secret():
    store = LocalSecretStore()
    t = ToolToken("u", "linear", store)
    t.save(TokenData.from_dict(_TOKEN_DATA))
    t.revoke()
    assert not t.exists()


def test_tool_token_mark_expired_emits_event():
    t = ToolToken("u", "linear", LocalSecretStore())
    cap = CapturingHandler()
    t.add_handler(cap)
    t.mark_expired()
    assert cap.events[0].type == EventType.TOOL_AUTH_EXPIRED


def test_tool_token_mark_expired_does_not_delete():
    store = LocalSecretStore()
    t = ToolToken("u", "linear", store)
    t.save(TokenData.from_dict(_TOKEN_DATA))
    t.mark_expired()
    assert t.exists()


def test_tool_token_mark_failed_emits_event():
    t = ToolToken("u", "linear", LocalSecretStore())
    cap = CapturingHandler()
    t.add_handler(cap)
    t.mark_failed("timeout")
    assert cap.events[0].type == EventType.TOOL_AUTH_FAILED
    assert cap.events[0].payload["reason"] == "timeout"
