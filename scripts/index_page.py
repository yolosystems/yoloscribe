# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3>=1.35.0"]
# ///
"""Index a single page — useful for testing the hybrid search indexer.

Usage:
    uv run --env-file .env python scripts/index_page.py <site> <page_path>

Examples:
    # Root page
    uv run --env-file .env python scripts/index_page.py knuth ""

    # Child page
    uv run --env-file .env python scripts/index_page.py knuth projects/yoloscribe/feature-backlog

Options:
    --enqueue   Send to SQS queue rather than running inline (production mode).
                Requires SQS_INDEXING_QUEUE_URL to be set.

In local mode (S3_ENDPOINT_URL set) the indexer runs inline by default.
In production mode (no S3_ENDPOINT_URL) use --enqueue to queue the job.
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
LOCAL_USER_ID = os.environ.get("LOCAL_USER_ID", "index-test")


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
    sqs_kwargs: dict = {"region_name": AWS_REGION}
    sqs_endpoint = os.environ.get("SQS_ENDPOINT_URL", "")
    if sqs_endpoint:
        sqs_kwargs["endpoint_url"] = sqs_endpoint
    sqs = session.client("sqs", **sqs_kwargs) if SQS_INDEXING_QUEUE_URL else None
    return s3, sqs


def main() -> None:
    parser = argparse.ArgumentParser(description="Index a single wiki page.")
    parser.add_argument("site", help='Site name, e.g. "knuth"')
    parser.add_argument("page_path", nargs="?", default="",
                        help='Page path, e.g. "projects/yoloscribe". Empty string for root page.')
    parser.add_argument("--enqueue", action="store_true",
                        help="Send to SQS instead of running inline")
    args = parser.parse_args()

    if not S3_BUCKET:
        log.error("S3_BUCKET env var is required")
        sys.exit(1)

    content_key = (
        f"{args.site}/{args.page_path}/content.md"
        if args.page_path
        else f"{args.site}/content.md"
    )

    log.info("Target: s3://%s/%s", S3_BUCKET, content_key)

    # Verify the page exists before doing anything
    s3, sqs = _make_clients()
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=content_key)
    except Exception:
        log.error("Page not found in S3: %s", content_key)
        sys.exit(1)

    if args.enqueue:
        if not sqs or not SQS_INDEXING_QUEUE_URL:
            log.error("--enqueue requires SQS_INDEXING_QUEUE_URL to be set")
            sys.exit(1)
        sqs.send_message(
            QueueUrl=SQS_INDEXING_QUEUE_URL,
            MessageBody=json.dumps({
                "bucket": S3_BUCKET,
                "content_key": content_key,
                "user_id": LOCAL_USER_ID,
            }),
        )
        log.info("Enqueued indexing job for %s", content_key)
    else:
        log.info("Running indexer inline...")
        sys.path.insert(0, str(Path(__file__).parent.parent / "indexer"))
        os.environ["BUCKET"] = S3_BUCKET
        os.environ["CONTENT_KEY"] = content_key
        os.environ["USER_ID"] = LOCAL_USER_ID
        from indexer.index_runner import main as index_main
        index_main()


if __name__ == "__main__":
    main()
