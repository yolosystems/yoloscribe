from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


# ── SecretStore interface ──────────────────────────────────────────────────────

class SecretStore(ABC):
    """Interface for reading and writing opaque secret strings."""

    @abstractmethod
    def get(self, key: str) -> str | None:
        """Return the secret at *key*, or None if not found."""

    @abstractmethod
    def put(self, key: str, value: str) -> None:
        """Create or overwrite the secret at *key*."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete *key*. No-op if it does not exist."""

    def exists(self, key: str) -> bool:
        return self.get(key) is not None


# ── Implementations ───────────────────────────────────────────────────────────

class SecretsManagerStore(SecretStore):
    def __init__(self, sm_client=None) -> None:
        if sm_client is None:
            import boto3
            sm_client = boto3.client("secretsmanager")
        self._sm = sm_client

    def get(self, key: str) -> str | None:
        try:
            resp = self._sm.get_secret_value(SecretId=key)
            return resp["SecretString"]
        except self._sm.exceptions.ResourceNotFoundException:
            return None
        except Exception as exc:
            log.warning("SecretsManager get failed for %s: %s", key, exc)
            return None

    def put(self, key: str, value: str) -> None:
        try:
            self._sm.put_secret_value(SecretId=key, SecretString=value)
        except self._sm.exceptions.ResourceNotFoundException:
            self._sm.create_secret(Name=key, SecretString=value)

    def delete(self, key: str) -> None:
        try:
            self._sm.delete_secret(SecretId=key, ForceDeleteWithoutRecovery=True)
        except self._sm.exceptions.ResourceNotFoundException:
            pass
        except Exception as exc:
            log.warning("SecretsManager delete failed for %s: %s", key, exc)


class SupabaseSecretStore(SecretStore):
    """Generic key-value secret store backed by a Supabase table.

    The table must have columns: key (text, primary key), value (text).
    """

    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        table: str = "user_secrets",
    ) -> None:
        self._base = supabase_url.rstrip("/")
        self._api_key = supabase_key
        self._table = table

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "apikey": self._api_key,
        }

    def get(self, key: str) -> str | None:
        qs = urllib.parse.urlencode({"key": f"eq.{key}", "select": "value", "limit": "1"})
        req = urllib.request.Request(
            f"{self._base}/rest/v1/{self._table}?{qs}",
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req) as resp:
                rows = json.loads(resp.read())
                return rows[0]["value"] if rows else None
        except Exception as exc:
            log.warning("SupabaseSecretStore get failed for %s: %s", key, exc)
            return None

    def put(self, key: str, value: str) -> None:
        data = json.dumps({"key": key, "value": value}).encode()
        req = urllib.request.Request(
            f"{self._base}/rest/v1/{self._table}",
            data=data,
            headers={
                **self._headers(),
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            method="POST",
        )
        urllib.request.urlopen(req)

    def delete(self, key: str) -> None:
        qs = urllib.parse.urlencode({"key": f"eq.{key}"})
        req = urllib.request.Request(
            f"{self._base}/rest/v1/{self._table}?{qs}",
            headers=self._headers(),
            method="DELETE",
        )
        try:
            urllib.request.urlopen(req)
        except Exception as exc:
            log.warning("SupabaseSecretStore delete failed for %s: %s", key, exc)


class LocalSecretStore(SecretStore):
    """In-memory secret store for testing — no external services required."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(initial or {})

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def put(self, key: str, value: str) -> None:
        self._store[key] = value

    def delete(self, key: str) -> None:
        self._store.pop(key, None)


# ── UserSecret base class ─────────────────────────────────────────────────────

class UserSecret(ABC):
    """Base class for per-user secrets. Subclasses encapsulate key path construction
    so callers never build raw secret store keys themselves."""

    def __init__(self, user_id: str, store: SecretStore) -> None:
        self._user_id = user_id
        self._store = store

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    @abstractmethod
    def _key(self) -> str:
        """Full secret store key — constructed by the subclass."""

    def get(self) -> str | None:
        return self._store.get(self._key)

    def put(self, value: str) -> None:
        self._store.put(self._key, value)

    def delete(self) -> None:
        self._store.delete(self._key)

    def exists(self) -> bool:
        return self.get() is not None
