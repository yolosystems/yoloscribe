"""Per-site SQLite FTS5 index cache.

Downloads {site}/.search/index.db from S3 and caches it locally, re-fetching
only when the S3 ETag changes or the 60-second TTL expires.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import threading
import time

log = logging.getLogger(__name__)

_TTL = 60  # seconds
_lock = threading.Lock()
_cache: dict[str, dict] = {}  # site → {path, etag, expires_at}
_tmp_dir = tempfile.mkdtemp(prefix="ys_fts_")


def _db_key(site: str) -> str:
    return f"{site}/.search/index.db"


def get_db_path(s3, bucket: str, site: str) -> str | None:
    """Return the local path to the cached FTS index, or None if unavailable."""
    with _lock:
        entry = _cache.get(site)
        now = time.time()

        if entry and now < entry["expires_at"] and os.path.exists(entry["path"]):
            return entry["path"]

        key = _db_key(site)
        local = os.path.join(_tmp_dir, f"{site.replace('/', '_')}.db")

        try:
            head = s3.head_object(Bucket=bucket, Key=key)
            etag = head.get("ETag", "")

            # Skip download if ETag unchanged and file already exists
            if entry and entry.get("etag") == etag and os.path.exists(local):
                _cache[site] = {"path": local, "etag": etag, "expires_at": now + _TTL}
                return local

            s3.download_file(bucket, key, local)
            _cache[site] = {"path": local, "etag": etag, "expires_at": now + _TTL}
            log.debug("Downloaded FTS index: site=%s etag=%s", site, etag)
            return local

        except Exception as exc:
            log.debug("FTS index unavailable for site=%s: %s", site, exc)
            return None


def fts_query(
    db_path: str,
    query: str,
    limit: int = 50,
    tags: list[str] | None = None,
    doc_type: str = "content",
) -> list[dict]:
    """Run an FTS5 query and return ranked results.

    Returns list of {"page_path": str, "excerpt": str, "rank": float,
                      "doc_type": str, "agent_name": str}.
    doc_type: "content" (default), "agent", or "all".
    """
    if not query.strip() and not tags:
        return []

    parts: list[str] = []
    if query.strip():
        parts.append(f'content : "{_escape(query)}"')
    if tags:
        for tag in tags:
            parts.append(f'tags : "{_escape(tag)}"')
    if doc_type != "all":
        parts.append(f'doc_type : "{_escape(doc_type)}"')

    match_expr = " ".join(parts)

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                """
                SELECT page_path,
                       snippet(fts, 4, '', '', '...', 20) AS excerpt,
                       rank,
                       doc_type,
                       agent_name
                FROM fts
                WHERE fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (match_expr, limit),
            ).fetchall()
            return [
                {"page_path": r[0], "excerpt": r[1], "rank": r[2], "doc_type": r[3], "agent_name": r[4]}
                for r in rows
            ]
        finally:
            conn.close()
    except Exception as exc:
        log.warning("FTS5 query failed: %s", exc)
        return []


def _escape(s: str) -> str:
    return s.replace('"', '""')


def fts_remove_pages(s3, bucket: str, site: str, page_paths: list[str]) -> None:
    """Remove pages from the FTS index, re-upload, and invalidate the local cache."""
    import tempfile
    key = f"{site}/.search/index.db"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    try:
        try:
            s3.download_file(bucket, key, db_path)
        except Exception:
            return  # No index yet — nothing to remove

        conn = sqlite3.connect(db_path)
        try:
            for pp in page_paths:
                conn.execute("DELETE FROM fts WHERE page_path = ?", (pp,))
            conn.commit()
        finally:
            conn.close()

        s3.upload_file(db_path, bucket, key, ExtraArgs={"ContentType": "application/x-sqlite3"})
        log.info("Removed %d pages from FTS index for site=%s", len(page_paths), site)

        with _lock:
            _cache.pop(site, None)
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass
