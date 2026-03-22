"""Pluggable secrets store — AWS Secrets Manager in production, S3 in local dev."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class SecretsStore(ABC):
    """Abstract interface for reading and writing secret strings."""

    @abstractmethod
    def get(self, key: str) -> str | None:
        """Return the secret string for *key*, or None if it does not exist."""

    @abstractmethod
    def put(self, key: str, value: str, description: str = "") -> None:
        """Create or overwrite the secret at *key* with *value*."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return True if the secret at *key* exists."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete the secret at *key*. No-op if it does not exist."""


class SecretsManagerStore(SecretsStore):
    """AWS Secrets Manager backend — identical to the pre-abstraction behaviour."""

    def __init__(self, sm_client) -> None:
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

    def put(self, key: str, value: str, description: str = "") -> None:
        try:
            self._sm.put_secret_value(SecretId=key, SecretString=value)
        except self._sm.exceptions.ResourceNotFoundException:
            self._sm.create_secret(Name=key, SecretString=value, Description=description)

    def exists(self, key: str) -> bool:
        return self.get(key) is not None

    def delete(self, key: str) -> None:
        try:
            self._sm.delete_secret(SecretId=key, ForceDeleteWithoutRecovery=True)
        except self._sm.exceptions.ResourceNotFoundException:
            pass
        except Exception as exc:
            log.warning("SecretsManager delete failed for %s: %s", key, exc)


class S3SecretsStore(SecretsStore):
    """S3 (MinIO) backend for local dev — stores secrets at _secrets/{key}.

    Values are stored as raw strings (typically JSON), same as Secrets Manager.
    This store is only ever used with LOCAL_MODE=true; MinIO is localhost-only
    with hardcoded dev credentials, so plaintext storage is acceptable.
    """

    def __init__(self, s3_client, bucket: str) -> None:
        self._s3 = s3_client
        self._bucket = bucket

    def _s3_key(self, key: str) -> str:
        return f"_secrets/{key}"

    def get(self, key: str) -> str | None:
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=self._s3_key(key))
            return obj["Body"].read().decode("utf-8")
        except Exception:
            return None

    def put(self, key: str, value: str, description: str = "") -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=self._s3_key(key),
            Body=value.encode("utf-8"),
            ContentType="application/json",
        )

    def exists(self, key: str) -> bool:
        return self.get(key) is not None

    def delete(self, key: str) -> None:
        try:
            self._s3.delete_object(Bucket=self._bucket, Key=self._s3_key(key))
        except Exception:
            pass


def make_secrets_store(
    local_mode: bool,
    s3_client,
    bucket: str,
    sm_client=None,
) -> SecretsStore:
    """Return the appropriate SecretsStore for the current environment."""
    if local_mode:
        return S3SecretsStore(s3_client, bucket)
    return SecretsManagerStore(sm_client)
