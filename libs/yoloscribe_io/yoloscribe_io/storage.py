from __future__ import annotations

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    """Abstract storage interface. Implementations must be safe to call from
    multiple threads but are not required to be atomic across calls."""

    @abstractmethod
    def read(self, key: str) -> str | None:
        """Return the decoded content at key, or None if not found."""

    @abstractmethod
    def read_with_etag(self, key: str) -> tuple[str, str] | tuple[None, None]:
        """Return (content, etag), or (None, None) if not found."""

    @abstractmethod
    def write(self, key: str, content: str, content_type: str = "text/markdown; charset=utf-8") -> None:
        """Unconditionally write content to key."""

    @abstractmethod
    def write_conditional(
        self,
        key: str,
        content: str,
        etag: str | None,
        content_type: str = "text/markdown; charset=utf-8",
    ) -> bool:
        """Write with optimistic concurrency. Returns True on success, False on conflict."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete key. No-op if key does not exist."""

    @abstractmethod
    def list(self, prefix: str) -> list[str]:
        """Return all keys that start with prefix."""


class S3StorageBackend(StorageBackend):
    def __init__(self, bucket: str, s3_client=None) -> None:
        if s3_client is None:
            import boto3
            s3_client = boto3.client("s3")
        self._s3 = s3_client
        self._bucket = bucket

    def read(self, key: str) -> str | None:
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
            return obj["Body"].read().decode("utf-8")
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise

    def read_with_etag(self, key: str) -> tuple[str, str] | tuple[None, None]:
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
            return obj["Body"].read().decode("utf-8"), obj.get("ETag", "")
        except Exception as exc:
            if _is_not_found(exc):
                return None, None
            raise

    def write(self, key: str, content: str, content_type: str = "text/markdown; charset=utf-8") -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType=content_type,
        )

    def write_conditional(
        self,
        key: str,
        content: str,
        etag: str | None,
        content_type: str = "text/markdown; charset=utf-8",
    ) -> bool:
        kwargs: dict = {"IfMatch": etag} if etag else {}
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content.encode("utf-8"),
                ContentType=content_type,
                **kwargs,
            )
            return True
        except Exception as exc:
            if _is_precondition_failed(exc):
                return False
            raise

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=key)

    def list(self, prefix: str) -> list[str]:
        resp = self._s3.list_objects_v2(Bucket=self._bucket, Prefix=prefix)
        return [obj["Key"] for obj in resp.get("Contents", [])]


class LocalStorageBackend(StorageBackend):
    """In-memory storage backend for testing — no AWS required."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(initial or {})
        self._etags: dict[str, str] = {}
        self._counter = 0

    def _mint_etag(self) -> str:
        self._counter += 1
        return f'"{self._counter}"'

    def read(self, key: str) -> str | None:
        return self._store.get(key)

    def read_with_etag(self, key: str) -> tuple[str, str] | tuple[None, None]:
        value = self._store.get(key)
        if value is None:
            return None, None
        return value, self._etags[key]

    def write(self, key: str, content: str, content_type: str = "text/markdown; charset=utf-8") -> None:
        self._store[key] = content
        self._etags[key] = self._mint_etag()

    def write_conditional(
        self,
        key: str,
        content: str,
        etag: str | None,
        content_type: str = "text/markdown; charset=utf-8",
    ) -> bool:
        if etag is not None and self._etags.get(key) != etag:
            return False
        self.write(key, content, content_type)
        return True

    def delete(self, key: str) -> None:
        self._store.pop(key, None)
        self._etags.pop(key, None)

    def list(self, prefix: str) -> list[str]:
        return [k for k in self._store if k.startswith(prefix)]


def _is_not_found(exc: Exception) -> bool:
    code = _error_code(exc)
    return code in ("NoSuchKey", "404", "NoSuchBucket")


def _is_precondition_failed(exc: Exception) -> bool:
    code = _error_code(exc)
    return code in ("PreconditionFailed", "412")


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    return response.get("Error", {}).get("Code", "")
