"""Index runner — K8s Job entry point for chunking and embedding content.md files.

Environment variables:
    BUCKET                      S3 bucket name
    CONTENT_KEY                 S3 key of the content.md to index (e.g. "knuth/content.md")
    USER_ID                     Supabase user UUID (passed in from the indexing queue message)
    S3_VECTORS_BUCKET           S3 Vectors bucket name
    S3_VECTORS_INDEX_NAME       S3 Vectors index name
    AWS_REGION                  AWS region
    BEDROCK_EMBEDDING_MODEL     Bedrock embedding model ID
    AWS_PROFILE                 (optional) named AWS profile for local development
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import time
from uuid import uuid4

import boto3

from .chunker import chunk_markdown
from .fts_index import download_or_create, update_page, upload

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

S3_VECTORS_BUCKET = os.environ.get("S3_VECTORS_BUCKET", "")
S3_VECTORS_INDEX_NAME = os.environ.get("S3_VECTORS_INDEX_NAME", "yoloscribe")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_EMBEDDING_MODEL = os.environ.get("BEDROCK_EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")
AWS_PROFILE = os.environ.get("AWS_PROFILE", "")

_MAX_BACKOFF_ATTEMPTS = 5


def _embed_with_retry(bedrock, text: str) -> list[float]:
    """Invoke Bedrock embedding with exponential backoff on ThrottlingException."""
    delay = 1.0
    for attempt in range(_MAX_BACKOFF_ATTEMPTS):
        try:
            resp = bedrock.invoke_model(
                modelId=BEDROCK_EMBEDDING_MODEL,
                body=json.dumps({"inputText": text}),
            )
            return json.loads(resp["body"].read())["embedding"]
        except bedrock.exceptions.ThrottlingException:
            if attempt == _MAX_BACKOFF_ATTEMPTS - 1:
                raise
            log.warning("Bedrock throttled (attempt %d/%d), retrying in %.1fs", attempt + 1, _MAX_BACKOFF_ATTEMPTS, delay)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("Unreachable")


def main() -> None:
    parser = argparse.ArgumentParser(description="Index a content.md file.")
    parser.add_argument("--bucket", default=os.environ.get("BUCKET", ""))
    parser.add_argument("--content-key", default=os.environ.get("CONTENT_KEY", ""))
    parser.add_argument("--user-id", default=os.environ.get("USER_ID", "unknown"))
    args = parser.parse_args()

    bucket = args.bucket
    content_key = args.content_key
    user_id = args.user_id

    if not bucket:
        parser.error("--bucket is required (or set BUCKET env var)")
    if not content_key:
        parser.error("--content-key is required (or set CONTENT_KEY env var)")
    if not S3_VECTORS_BUCKET:
        parser.error("S3_VECTORS_BUCKET env var is required")

    start = time.time()

    session = boto3.Session(profile_name=AWS_PROFILE or None)
    s3 = session.client("s3", region_name=AWS_REGION)
    bedrock = session.client("bedrock-runtime", region_name=AWS_REGION)
    s3vectors = session.client("s3vectors", region_name=AWS_REGION)

    # Derive site and page directory from content key
    # e.g. "knuth/blog/content.md" → site="knuth", page_dir="knuth/blog"
    site = content_key.split("/")[0]
    page_dir = content_key.rsplit("/", 1)[0]
    # page_path is the wiki path: strip site prefix and trailing /content.md
    relative = content_key[len(site) + 1:]  # e.g. "blog/content.md" or "content.md"
    page_path = "" if relative == "content.md" else relative[:-len("/content.md")]

    log.info("Indexing: site=%s page_path=%r user_id=%s", site, page_path, user_id)

    # Fetch content
    content = s3.get_object(Bucket=bucket, Key=content_key)["Body"].read().decode("utf-8")

    # Delete existing chunks and vectors
    chunks_prefix = f"{page_dir}/.chunks/"
    paginator = s3.get_paginator("list_objects_v2")
    existing_chunk_keys = []
    existing_vector_ids = []
    for page in paginator.paginate(Bucket=bucket, Prefix=chunks_prefix):
        for obj in page.get("Contents", []):
            existing_chunk_keys.append({"Key": obj["Key"]})
            existing_vector_ids.append(obj["Key"].split("/")[-1])
    if existing_chunk_keys:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": existing_chunk_keys, "Quiet": True})
        log.info("Deleted %d existing chunk objects", len(existing_chunk_keys))
    if existing_vector_ids:
        try:
            for i in range(0, len(existing_vector_ids), 100):
                s3vectors.delete_vectors(
                    vectorBucketName=S3_VECTORS_BUCKET,
                    indexName=S3_VECTORS_INDEX_NAME,
                    keys=existing_vector_ids[i:i + 100],
                )
            log.info("Deleted %d existing vectors", len(existing_vector_ids))
        except Exception as exc:
            log.warning("Failed to delete existing vectors (continuing): %s", exc)

    # Chunk with markdown-aware strategy
    chunks = chunk_markdown(content)
    log.info("Produced %d chunks from %s", len(chunks), content_key)

    if not chunks:
        log.info("No content to index — updating FTS with empty page and exiting.")
        _update_fts(s3, bucket, site, page_path, [])
        return

    # Embed and store each chunk in S3 + S3 Vectors
    stored = 0
    for chunk in chunks:
        chunk_id = str(uuid4())
        chunk_data = {"text": chunk["text"], "tags": chunk["tags"], "source": content_key}
        s3.put_object(
            Bucket=bucket,
            Key=f"{page_dir}/.chunks/{chunk_id}",
            Body=json.dumps(chunk_data).encode("utf-8"),
            ContentType="application/json",
        )
        try:
            embedding = _embed_with_retry(bedrock, chunk["text"])
        except Exception as exc:
            log.error("Failed to embed chunk %s: %s — skipping", chunk_id, exc)
            continue
        try:
            s3vectors.put_vectors(
                vectorBucketName=S3_VECTORS_BUCKET,
                indexName=S3_VECTORS_INDEX_NAME,
                vectors=[{
                    "key": chunk_id,
                    "data": {"float32": embedding},
                    "metadata": {
                        "user_id": user_id,
                        "path": content_key,
                        "tags": chunk["tags"],
                    },
                }],
            )
            stored += 1
        except Exception as exc:
            log.error("Failed to store vector for chunk %s: %s — skipping", chunk_id, exc)

    # Build / update SQLite FTS5 index
    _update_fts(s3, bucket, site, page_path, chunks)

    elapsed = time.time() - start
    log.info(
        "Indexing complete: site=%s page=%r chunks=%d stored=%d elapsed=%.1fs",
        site, page_path, len(chunks), stored, elapsed,
    )


def _update_fts(s3, bucket: str, site: str, page_path: str, chunks: list[dict]) -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    try:
        download_or_create(s3, bucket, site, db_path)
        update_page(db_path, page_path, chunks)
        upload(s3, bucket, site, db_path)
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
