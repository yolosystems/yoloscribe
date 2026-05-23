from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .events import EventEmitter, EventType
from .secrets import SecretStore, UserSecret
from .storage import StorageBackend

log = logging.getLogger(__name__)


# ── OAuthClientConfig ─────────────────────────────────────────────────────────

@dataclass
class OAuthClientConfig:
    """Parsed representation of .tools/{name}/oauth_client.json."""

    client_id: str = ""
    scopes: list[str] = field(default_factory=list)
    issuer: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OAuthClientConfig:
        known = {"client_id", "scopes", "issuer"}
        return cls(
            client_id=str(d.get("client_id", "")),
            scopes=list(d.get("scopes", [])),
            issuer=str(d.get("issuer", "")),
            extra={k: v for k, v in d.items() if k not in known},
        )


# ── ToolConfig ────────────────────────────────────────────────────────────────

@dataclass
class ToolConfig:
    """Parsed representation of a tool at .tools/{name}/.

    Reads mcp.json (required) and oauth_client.json (optional) from a
    bucket-scoped StorageBackend. The storage backend must be scoped to the
    bucket root so paths like .tools/{name}/mcp.json resolve correctly.
    """

    name: str
    transport: str = ""
    url: str = ""
    command: str = ""
    requires_oauth: bool = False
    oauth_client: OAuthClientConfig | None = None
    raw_mcp: dict[str, Any] = field(default_factory=dict)


def load_tool_config(
    name: str,
    storage: StorageBackend,
) -> ToolConfig | None:
    """Load ToolConfig from .tools/{name}/ in *storage*.

    Returns None if mcp.json does not exist. Never raises on malformed JSON.
    """
    mcp_key = f".tools/{name}/mcp.json"
    raw = storage.read(mcp_key)
    if raw is None:
        return None

    try:
        mcp = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("tool %s: malformed mcp.json", name)
        mcp = {}

    servers = mcp.get("mcpServers", {})
    server_cfg = servers.get(name) or (next(iter(servers.values())) if servers else {})

    transport = str(server_cfg.get("transport", ""))
    url = str(server_cfg.get("url", ""))
    command = str(server_cfg.get("command", ""))
    requires_oauth = str(server_cfg.get("auth", "")).lower() == "oauth"

    oauth_client: OAuthClientConfig | None = None
    oauth_key = f".tools/{name}/oauth_client.json"
    oauth_raw = storage.read(oauth_key)
    if oauth_raw is not None:
        try:
            oauth_client = OAuthClientConfig.from_dict(json.loads(oauth_raw))
        except (json.JSONDecodeError, Exception):
            log.warning("tool %s: malformed oauth_client.json", name)

    return ToolConfig(
        name=name,
        transport=transport,
        url=url,
        command=command,
        requires_oauth=requires_oauth,
        oauth_client=oauth_client,
        raw_mcp=mcp,
    )


def list_tools(storage: StorageBackend) -> list[str]:
    """Return tool names found under .tools/ in *storage*."""
    names: list[str] = []
    seen: set[str] = set()
    for key in storage.list(".tools/"):
        parts = key.split("/")
        if len(parts) >= 2 and parts[0] == ".tools" and parts[1]:
            if parts[1] not in seen:
                seen.add(parts[1])
                names.append(parts[1])
    return sorted(names)


# ── ToolToken ─────────────────────────────────────────────────────────────────

@dataclass
class TokenData:
    """Parsed token payload stored in Secrets Manager."""

    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    client_id: str = ""
    client_secret: str | None = None
    scope: str = ""
    auth_server_metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TokenData:
        return cls(
            access_token=str(d.get("access_token", "")),
            refresh_token=str(d.get("refresh_token", "")),
            expires_at=int(d.get("expires_at", 0)),
            client_id=str(d.get("client_id", "")),
            client_secret=d.get("client_secret"),
            scope=str(d.get("scope", "")),
            auth_server_metadata=dict(d.get("auth_server_metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "client_id": self.client_id,
            "scope": self.scope,
            "auth_server_metadata": self.auth_server_metadata,
        }
        if self.client_secret is not None:
            d["client_secret"] = self.client_secret
        return d


class ToolToken(UserSecret, EventEmitter):
    """OAuth token for a named tool, stored at yoloscribe/{user_id}/oauth/{tool_name}.

    Inherits UserSecret (secret-store CRUD) and EventEmitter (auth lifecycle events).
    """

    def __init__(self, user_id: str, tool_name: str, store: SecretStore) -> None:
        UserSecret.__init__(self, user_id, store)
        EventEmitter.__init__(self)
        self._tool_name = tool_name

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def _key(self) -> str:
        return f"yoloscribe/{self._user_id}/oauth/{self._tool_name}"

    # ── Token data helpers ────────────────────────────────────────────────────

    def load(self) -> TokenData | None:
        """Parse and return the stored token, or None if absent or malformed."""
        raw = self.get()
        if raw is None:
            return None
        try:
            return TokenData.from_dict(json.loads(raw))
        except (json.JSONDecodeError, Exception) as exc:
            log.warning("ToolToken.load failed for %s/%s: %s", self._user_id, self._tool_name, exc)
            return None

    def save(self, data: TokenData) -> None:
        """Persist *data* and emit tool.auth_started → tool.auth_completed."""
        self._emit(EventType.TOOL_AUTH_STARTED, {
            "user_id": self._user_id,
            "tool_name": self._tool_name,
        })
        self.put(json.dumps(data.to_dict()))
        self._emit(EventType.TOOL_AUTH_COMPLETED, {
            "user_id": self._user_id,
            "tool_name": self._tool_name,
        })

    def revoke(self) -> None:
        """Delete the stored token and emit tool.auth_revoked."""
        self.delete()
        self._emit(EventType.TOOL_AUTH_REVOKED, {
            "user_id": self._user_id,
            "tool_name": self._tool_name,
        })

    def mark_expired(self) -> None:
        """Emit tool.auth_expired without deleting the token (caller may refresh)."""
        self._emit(EventType.TOOL_AUTH_EXPIRED, {
            "user_id": self._user_id,
            "tool_name": self._tool_name,
        })

    def mark_failed(self, reason: str = "") -> None:
        """Emit tool.auth_failed (e.g. refresh failed, token unusable)."""
        self._emit(EventType.TOOL_AUTH_FAILED, {
            "user_id": self._user_id,
            "tool_name": self._tool_name,
            "reason": reason,
        })
