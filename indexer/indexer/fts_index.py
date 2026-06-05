"""SQLite FTS5 index builder and S3 uploader.

The index lives at {site}/.search/index.db in the content S3 bucket.
On each page write the indexer downloads the existing database (if any),
updates the rows for the current page, then re-uploads.

Concurrency note: two indexer jobs for the same site can race here, with one
overwriting the other's update. The last upload wins and the losing update is
recovered on the next write to that page. This is acceptable for a search index.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile

log = logging.getLogger(__name__)

_FTS_KEY_SUFFIX = ".search/index.db"

_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    page_path UNINDEXED,
    chunk_id  UNINDEXED,
    content,
    tags,
    tokenize  = 'porter unicode61'
);
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def db_s3_key(site: str) -> str:
    return f"{site}/{_FTS_KEY_SUFFIX}"


def download_or_create(s3, bucket: str, site: str, local_path: str) -> None:
    """Download existing index.db to local_path, or create a fresh empty one."""
    key = db_s3_key(site)
    try:
        s3.download_file(bucket, key, local_path)
        log.info("Downloaded FTS index from s3://%s/%s", bucket, key)
    except Exception:
        log.info("No existing FTS index — creating fresh at s3://%s/%s", bucket, key)
        _init_db(local_path)


def _init_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(_CREATE_FTS)
    conn.execute(_CREATE_META)
    conn.commit()
    conn.close()


def update_page(db_path: str, page_path: str, chunks: list[dict]) -> None:
    """Replace all FTS rows for page_path with the new chunks."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_CREATE_FTS)
        conn.execute(_CREATE_META)
        conn.execute("DELETE FROM fts WHERE page_path = ?", (page_path,))
        for i, chunk in enumerate(chunks):
            tags_str = " ".join(chunk.get("tags", []))
            conn.execute(
                "INSERT INTO fts(page_path, chunk_id, content, tags) VALUES (?, ?, ?, ?)",
                (page_path, f"{page_path}#{i}", chunk["text"], tags_str),
            )
        conn.commit()
        log.info("Updated FTS index: page=%s chunks=%d", page_path, len(chunks))
    finally:
        conn.close()


def upload(s3, bucket: str, site: str, db_path: str) -> None:
    """Upload the SQLite database to S3."""
    key = db_s3_key(site)
    s3.upload_file(db_path, bucket, key, ExtraArgs={"ContentType": "application/x-sqlite3"})
    log.info("Uploaded FTS index to s3://%s/%s", bucket, key)
