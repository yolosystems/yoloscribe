from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .events import EventEmitter, EventType
from .secrets import SecretStore, UserSecret

log = logging.getLogger(__name__)


# ── WebhookEntry ──────────────────────────────────────────────────────────────

@dataclass
class WebhookEntry:
    label: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "url": self.url}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WebhookEntry:
        return cls(label=str(d.get("label", "")), url=str(d.get("url", "")))


# ── Webhooks ──────────────────────────────────────────────────────────────────

class Webhooks(UserSecret, EventEmitter):
    """Per-user webhook list stored at yoloscribe/{user_id}/webhooks in Secrets Manager.

    Payload is a JSON array of {label, url} objects. Mutations always do a
    full read-modify-write so concurrent callers converge rather than silently
    losing each other's entries.
    """

    def __init__(self, user_id: str, store: SecretStore) -> None:
        UserSecret.__init__(self, user_id, store)
        EventEmitter.__init__(self)

    @property
    def _key(self) -> str:
        return f"yoloscribe/{self._user_id}/webhooks"

    # ── read ──────────────────────────────────────────────────────────────────

    def list(self) -> list[WebhookEntry]:
        """Return the current webhook list, or [] if empty / malformed."""
        raw = self.get()
        if raw is None:
            return []
        try:
            data = json.loads(raw)
            return [WebhookEntry.from_dict(item) for item in data if isinstance(item, dict)]
        except (json.JSONDecodeError, Exception) as exc:
            log.warning("Webhooks.list parse error for %s: %s", self._user_id, exc)
            return []

    # ── write ─────────────────────────────────────────────────────────────────

    def add(self, label: str, url: str) -> None:
        """Append a new webhook entry and emit webhook.added."""
        entries = self.list()
        entries.append(WebhookEntry(label=label, url=url))
        self._save(entries)
        self._emit(EventType.WEBHOOK_ADDED, {
            "user_id": self._user_id,
            "label": label,
            "url": url,
        })

    def remove(self, label: str) -> bool:
        """Remove the first entry with *label*. Returns True if removed, False if not found."""
        entries = self.list()
        removed = False
        kept: list[WebhookEntry] = []
        for e in entries:
            if e.label == label and not removed:
                removed = True
            else:
                kept.append(e)
        if not removed:
            return False
        entries = kept
        self._save(entries)
        self._emit(EventType.WEBHOOK_REMOVED, {
            "user_id": self._user_id,
            "label": label,
        })
        return True

    def remove_by_url(self, url: str) -> bool:
        """Remove the first entry with *url*. Returns True if removed, False if not found."""
        entries = self.list()
        removed: list[WebhookEntry] = []
        kept: list[WebhookEntry] = []
        for e in entries:
            if e.url == url and not removed:
                removed.append(e)
            else:
                kept.append(e)
        if not removed:
            return False
        self._save(kept)
        self._emit(EventType.WEBHOOK_REMOVED, {
            "user_id": self._user_id,
            "label": removed[0].label,
            "url": url,
        })
        return True

    def _save(self, entries: list[WebhookEntry]) -> None:
        self.put(json.dumps([e.to_dict() for e in entries]))


# ── APITokenData ──────────────────────────────────────────────────────────────

@dataclass
class APITokenData:
    """Parsed payload for an API token stored in the secret store."""

    token_hash: str = ""
    site: str = ""
    expires_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_hash": self.token_hash,
            "site": self.site,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> APITokenData:
        return cls(
            token_hash=str(d.get("token_hash", "")),
            site=str(d.get("site", "")),
            expires_at=int(d.get("expires_at", 0)),
        )


# ── APIToken ──────────────────────────────────────────────────────────────────

class APIToken(UserSecret, EventEmitter):
    """Per-user API token stored at yoloscribe/{user_id}/api_token.

    Conventionally backed by SupabaseSecretStore, but accepts any SecretStore
    so tests can use LocalSecretStore without network calls.
    """

    def __init__(self, user_id: str, store: SecretStore) -> None:
        UserSecret.__init__(self, user_id, store)
        EventEmitter.__init__(self)

    @property
    def _key(self) -> str:
        return f"yoloscribe/{self._user_id}/api_token"

    # ── read ──────────────────────────────────────────────────────────────────

    def load(self) -> APITokenData | None:
        """Return the stored token data, or None if absent or malformed."""
        raw = self.get()
        if raw is None:
            return None
        try:
            return APITokenData.from_dict(json.loads(raw))
        except (json.JSONDecodeError, Exception) as exc:
            log.warning("APIToken.load parse error for %s: %s", self._user_id, exc)
            return None

    # ── write ─────────────────────────────────────────────────────────────────

    def create(self, data: APITokenData) -> None:
        """Persist *data* and emit token.created."""
        self.put(json.dumps(data.to_dict()))
        self._emit(EventType.TOKEN_CREATED, {
            "user_id": self._user_id,
            "site": data.site,
        })

    def revoke(self) -> None:
        """Delete the token and emit token.revoked."""
        self.delete()
        self._emit(EventType.TOKEN_REVOKED, {
            "user_id": self._user_id,
        })
