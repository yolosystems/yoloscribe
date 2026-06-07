# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3>=1.35.0"]
# ///
"""One-time cleanup: purge .user/ entries from the search index.

The indexer previously indexed content under {site}/.user/ (e.g. ingest staging
files). Those paths are platform/system files and should never appear in search
results. This script removes them from S3 Vectors and the per-site SQLite FTS
index, and deletes the orphaned .chunks/ objects from S3.

Usage:
    uv run --env-file .env python scripts/purge_user_index_entries.py [--dry-run] [--site SITE]

Options:
    --dry-run   List affected entries without deleting anything.
    --site SITE Restrict to a single named site.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_VECTORS_BUCKET = os.environ.get("S3_VECTORS_BUCKET", "")
S3_VECTORS_INDEX_NAME = os.environ.get("S3_VECTORS_INDEX_NAME", "yoloscribe")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_PROFILE = os.environ.get("AWS_PROFILE", "")


def _make_clients():
    import boto3
    session = boto3.Session(profile_name=AWS_PROFILE or None)
    s3_kwargs: dict = {"region_name": AWS_REGION}
    if S3_ENDPOINT_URL:
        s3_kwargs["endpoint_url"] = S3_ENDPOINT_URL
        minio_key = os.environ.get("MINIO_ACCESS_KEY_ID")
        minio_secret = os.environ.get("MINIO_SECRET_ACCESS_KEY")
        if minio_key and minio_secret:
            s3_kwargs["aws_access_key_id"] = minio_key
            s3_kwargs["aws_secret_access_key"] = minio_secret
    s3 = session.client("s3", **s3_kwargs)
    s3vectors = session.client("s3vectors", region_name=AWS_REGION) if S3_VECTORS_BUCKET else None
    return s3, s3vectors


def _find_user_chunks(
    s3, bucket: str, site_filter: str | None
) -> dict[str, list[tuple[str, str, str]]]:
    """Scan S3 for chunk objects stored under .user/ paths.

    Returns {site: [(s3_key, chunk_id, page_path), ...]} where page_path is the
    wiki path that was indexed (e.g. '.user/ingest').
    """
    paginator = s3.get_paginator("list_objects_v2")
    prefix = f"{site_filter}/" if site_filter else ""
    result: dict[str, list[tuple[str, str, str]]] = defaultdict(list)

    for s3_page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in s3_page.get("Contents", []):
            key = obj["Key"]
            parts = key.split("/")
            # Need at least: site / .user / something / .chunks / chunk_id
            if len(parts) < 5:
                continue
            if parts[1] != ".user":
                continue
            if ".chunks" not in parts:
                continue
            chunks_idx = parts.index(".chunks")
            site = parts[0]
            chunk_id = parts[-1]
            # page_path: from .user/ up to (not including) .chunks/
            page_path = "/".join(parts[1:chunks_idx])
            result[site].append((key, chunk_id, page_path))

    return dict(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="Purge .user/ entries from the search index.")
    parser.add_argument("--dry-run", action="store_true", help="List affected entries without deleting")
    parser.add_argument("--site", default="", help="Restrict to a single site")
    args = parser.parse_args()

    if not S3_BUCKET:
        log.error("S3_BUCKET env var is required")
        sys.exit(1)

    s3, s3vectors = _make_clients()
    chunks_by_site = _find_user_chunks(s3, S3_BUCKET, args.site or None)

    if not chunks_by_site:
        log.info("No .user/ index entries found — nothing to purge.")
        return

    total = sum(len(v) for v in chunks_by_site.values())
    log.info("Found %d .user/ chunk(s) across %d site(s)", total, len(chunks_by_site))

    if args.dry_run:
        for site, chunks in sorted(chunks_by_site.items()):
            for s3_key, chunk_id, page_path in chunks:
                print(f"  [{site}] page_path={page_path!r}  chunk={chunk_id}")
        return

    for site, chunks in sorted(chunks_by_site.items()):
        log.info("Purging site %r (%d chunk(s))", site, len(chunks))
        s3_keys = [{"Key": s3_key} for s3_key, _, _ in chunks]
        chunk_ids = [chunk_id for _, chunk_id, _ in chunks]
        page_paths = sorted({page_path for _, _, page_path in chunks})

        # 1. Delete vectors from S3 Vectors
        if s3vectors and S3_VECTORS_BUCKET:
            for i in range(0, len(chunk_ids), 100):
                batch = chunk_ids[i:i + 100]
                try:
                    s3vectors.delete_vectors(
                        vectorBucketName=S3_VECTORS_BUCKET,
                        indexName=S3_VECTORS_INDEX_NAME,
                        keys=batch,
                    )
                    log.info("Deleted %d vector(s) for site %r", len(batch), site)
                except Exception as exc:
                    log.warning("Failed to delete vectors for site %r: %s", site, exc)
        else:
            log.info("S3_VECTORS_BUCKET not configured — skipping vector deletion")

        # 2. Delete S3 chunk objects
        for i in range(0, len(s3_keys), 1000):
            batch = s3_keys[i:i + 1000]
            try:
                s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": batch, "Quiet": True})
                log.info("Deleted %d chunk object(s) for site %r", len(batch), site)
            except Exception as exc:
                log.warning("Failed to delete chunk objects for site %r: %s", site, exc)

        # 3. Clear affected page_paths from the FTS index
        _purge_fts(s3, S3_BUCKET, site, page_paths)

    log.info("Purge complete.")


def _purge_fts(s3, bucket: str, site: str, page_paths: list[str]) -> None:
    sys.path.insert(0, str(Path(__file__).parent.parent / "indexer"))
    from indexer.fts_index import download_or_create, update_page, upload

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    try:
        download_or_create(s3, bucket, site, db_path)
        for page_path in page_paths:
            update_page(db_path, page_path, [])
            log.info("Cleared FTS entries: site=%r page=%r", site, page_path)
        upload(s3, bucket, site, db_path)
    except Exception as exc:
        log.warning("Failed to update FTS for site %r: %s", site, exc)
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
