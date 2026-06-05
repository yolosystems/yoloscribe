# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3>=1.35.0"]
# ///
"""One-time migration: re-index all content pages for all sites.

Re-chunks every content.md using the new markdown-aware chunking strategy,
rebuilds the SQLite FTS5 index per site, and re-embeds all chunks in S3 Vectors.

Usage:
    uv run --env-file .env python scripts/reindex_all.py [--dry-run] [--site SITE]

Options:
    --dry-run       List pages that would be re-indexed without enqueuing jobs.
    --site SITE     Re-index only a single named site.
    --concurrency N Max parallel indexing jobs (default 4, local mode only).

In production, this script enqueues an SQS indexing job per page, identical
to the messages sent on a normal page write. The existing indexer Kubernetes
Jobs consume them and handle the actual re-indexing.

For LOCAL_MODE (S3_ENDPOINT_URL set), the re-indexing runs inline.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")
SQS_INDEXING_QUEUE_URL = os.environ.get("SQS_INDEXING_QUEUE_URL", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_PROFILE = os.environ.get("AWS_PROFILE", "")
LOCAL_USER_ID = os.environ.get("LOCAL_USER_ID", "reindex-migration")


def _make_clients():
    import boto3
    session = boto3.Session(profile_name=AWS_PROFILE or None)
    s3_kwargs = {"region_name": AWS_REGION}
    if S3_ENDPOINT_URL:
        s3_kwargs["endpoint_url"] = S3_ENDPOINT_URL
        minio_key = os.environ.get("MINIO_ACCESS_KEY_ID")
        minio_secret = os.environ.get("MINIO_SECRET_ACCESS_KEY")
        if minio_key and minio_secret:
            s3_kwargs["aws_access_key_id"] = minio_key
            s3_kwargs["aws_secret_access_key"] = minio_secret
    s3 = session.client("s3", **s3_kwargs)
    sqs_kwargs = {"region_name": AWS_REGION}
    sqs_endpoint = os.environ.get("SQS_ENDPOINT_URL", "")
    if sqs_endpoint:
        sqs_kwargs["endpoint_url"] = sqs_endpoint
    sqs = session.client("sqs", **sqs_kwargs) if SQS_INDEXING_QUEUE_URL else None
    return s3, sqs


def _list_content_pages(s3, bucket: str, site_filter: str | None) -> list[tuple[str, str]]:
    """Return list of (site, content_key) for all non-internal content pages."""
    paginator = s3.get_paginator("list_objects_v2")
    pages = []
    prefix = f"{site_filter}/" if site_filter else ""
    for s3_page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in s3_page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/content.md"):
                continue
            # Skip internal paths
            parts = key.split("/")
            if any(p.startswith(".") for p in parts[:-1]):
                continue
            site = parts[0]
            pages.append((site, key))
    return pages


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-index all wiki content pages.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--site", default="")
    args = parser.parse_args()

    if not S3_BUCKET:
        log.error("S3_BUCKET env var is required")
        sys.exit(1)

    s3, sqs = _make_clients()
    pages = _list_content_pages(s3, S3_BUCKET, args.site or None)
    log.info("Found %d content pages to re-index", len(pages))

    if args.dry_run:
        for site, key in pages:
            print(f"  {key}")
        return

    enqueued = 0
    for site, content_key in pages:
        if args.dry_run:
            log.info("[dry-run] would enqueue: %s", content_key)
            continue

        if sqs and SQS_INDEXING_QUEUE_URL:
            sqs.send_message(
                QueueUrl=SQS_INDEXING_QUEUE_URL,
                MessageBody=json.dumps({
                    "bucket": S3_BUCKET,
                    "content_key": content_key,
                    "user_id": LOCAL_USER_ID,
                }),
            )
            enqueued += 1
            if enqueued % 50 == 0:
                log.info("Enqueued %d / %d jobs...", enqueued, len(pages))
        else:
            # Local mode: run inline
            log.info("Re-indexing inline: %s", content_key)
            try:
                _run_inline(content_key)
            except Exception as exc:
                log.error("Failed to re-index %s: %s", content_key, exc)

    log.info("Done. Enqueued %d indexing jobs.", enqueued)


def _run_inline(content_key: str) -> None:
    """Run the indexer inline for local mode."""
    sys.path.insert(0, str(Path(__file__).parent.parent / "indexer"))
    os.environ["BUCKET"] = S3_BUCKET
    os.environ["CONTENT_KEY"] = content_key
    os.environ["USER_ID"] = LOCAL_USER_ID
    from indexer.index_runner import main as index_main
    index_main()


if __name__ == "__main__":
    main()
