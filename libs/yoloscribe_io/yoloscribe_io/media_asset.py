from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from .events import EventEmitter, EventType
from .provenance import Provenance
from .storage import StorageBackend

log = logging.getLogger(__name__)


class MediaAsset(EventEmitter):
    """Page-scoped binary asset backed by StorageBackend.

    Stores metadata as JSON at {site}/{page_path}/.media/{filename}.json.
    The binary bytes are managed by the caller (e.g. via S3 presigned URLs);
    this class tracks the metadata and emits lifecycle events.

    Events:
        page.media_added   — emitted by register()
        page.media_removed — emitted by remove()
    """

    def __init__(
        self,
        site: str,
        page_path: str,
        filename: str,
        storage: StorageBackend,
        *,
        mime_type: str = "",
        size_bytes: int = 0,
        cdn_url: str = "",
        provenance: "Provenance | None" = None,
    ) -> None:
        super().__init__()
        self._site = site
        self._page_path = page_path
        self._filename = filename
        self._storage = storage
        self._mime_type = mime_type
        self._size_bytes = size_bytes
        self._cdn_url = cdn_url
        self._provenance = provenance

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def site(self) -> str:
        return self._site

    @property
    def page_path(self) -> str:
        return self._page_path

    @property
    def filename(self) -> str:
        return self._filename

    @property
    def provenance(self) -> Provenance | None:
        """Where this asset came from, when it arrived through ingest (YOL-552).

        None for assets uploaded straight onto a page — those have no source
        beyond the person who attached them.
        """
        return self._provenance

    @property
    def mime_type(self) -> str:
        return self._mime_type

    @property
    def size_bytes(self) -> int:
        return self._size_bytes

    @property
    def cdn_url(self) -> str:
        return self._cdn_url

    @property
    def key(self) -> str:
        """Storage key for this asset's metadata JSON."""
        base = f"{self._site}/{self._page_path}" if self._page_path else self._site
        return f"{base}/.media/{self._filename}.json"

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def register(self) -> None:
        """Persist metadata and emit page.media_added."""
        record: dict = {
            "filename": self._filename,
            "mime_type": self._mime_type,
            "size_bytes": self._size_bytes,
            "cdn_url": self._cdn_url,
        }
        # Nested rather than flattened so a provenance field can never collide
        # with a media field, and so `"provenance" in record` is the test for
        # "did this arrive through ingest" (YOL-552).
        if self._provenance is not None:
            record["provenance"] = self._provenance.to_dict()
        self._storage.write(self.key, json.dumps(record), content_type="application/json")
        self._emit(EventType.PAGE_MEDIA_ADDED, {
            "site": self._site,
            "page_path": self._page_path,
            "filename": self._filename,
            "mime_type": self._mime_type,
            "size_bytes": self._size_bytes,
            "cdn_url": self._cdn_url,
        })

    def remove(self) -> None:
        """Delete metadata and emit page.media_removed."""
        self._storage.delete(self.key)
        self._emit(EventType.PAGE_MEDIA_REMOVED, {
            "site": self._site,
            "page_path": self._page_path,
            "filename": self._filename,
        })

    def exists(self) -> bool:
        return self._storage.read(self.key) is not None


# ── helpers ───────────────────────────────────────────────────────────────────

def load_media_asset(
    site: str,
    page_path: str,
    filename: str,
    storage: StorageBackend,
) -> MediaAsset | None:
    """Load a MediaAsset from stored metadata. Returns None if not found."""
    base = f"{site}/{page_path}" if page_path else site
    key = f"{base}/.media/{filename}.json"
    raw = storage.read(key)
    if raw is None:
        return None
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("load_media_asset: malformed JSON at %s: %s", key, exc)
        return None
    raw_prov = d.get("provenance")
    return MediaAsset(
        site=site,
        page_path=page_path,
        filename=filename,
        storage=storage,
        mime_type=str(d.get("mime_type", "")),
        size_bytes=int(d.get("size_bytes", 0)),
        cdn_url=str(d.get("cdn_url", "")),
        provenance=Provenance.from_dict(raw_prov) if isinstance(raw_prov, dict) else None,
    )


def list_page_media(
    site: str,
    page_path: str,
    storage: StorageBackend,
) -> list[MediaAsset]:
    """Return all registered MediaAssets for *page_path* on *site*."""
    base = f"{site}/{page_path}" if page_path else site
    prefix = f"{base}/.media/"
    assets: list[MediaAsset] = []
    for key in storage.list(prefix):
        if not key.endswith(".json"):
            continue
        filename = key[len(prefix):-len(".json")]
        if not filename:
            continue
        asset = load_media_asset(site, page_path, filename, storage)
        if asset is not None:
            assets.append(asset)
    return assets
