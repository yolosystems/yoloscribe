# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3>=1.35.0"]
# ///
"""Index a single page — useful for testing the hybrid search indexer.

Usage:
    uv run --env-file .env scripts/index_page.py <site> [<page_path>] [--enqueue]

    NOTE: omit the `python` keyword — uv needs to see the script path directly
    to pick up the inline dependency block above.

Examples:
    # Child page (inline — runs the indexer in a subprocess via the indexer venv)
    uv run --env-file .env scripts/index_page.py knuth cross-page-agent-test

    # Root page
    uv run --env-file .env scripts/index_page.py knuth

    # Enqueue to SQS instead of running inline (production mode)
    uv run --env-file .env scripts/index_page.py knuth cross-page-agent-test --enqueue

Options:
    --enqueue   Send to SQS queue rather than running inline.
                Requires SQS_INDEXING_QUEUE_URL to be set.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
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

_REPO_ROOT = Path(__file__).parent.parent
_INDEXER_DIR = _REPO_ROOT / "indexer"
_ENV_FILE = _REPO_ROOT / ".env"


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
        # Run the indexer via subprocess so it uses the indexer's own venv
        # (which has langchain-text-splitters and all other indexer deps).
        log.info("Running indexer inline via indexer venv...")
        env_args = ["--env-file", str(_ENV_FILE)] if _ENV_FILE.exists() else []
        result = subprocess.run(
            ["uv"] + env_args + ["run", "index-runner",
               "--bucket", S3_BUCKET,
               "--content-key", content_key,
               "--user-id", LOCAL_USER_ID],
            cwd=str(_INDEXER_DIR),
        )
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
