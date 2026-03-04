"""Index runner — K8s Job entry point for chunking and embedding content.md files.

Environment variables:
    BUCKET                      S3 bucket name
    CONTENT_KEY                 S3 key of the content.md to index (e.g. "knuth/content.md")
    S3_VECTORS_BUCKET           S3 Vectors bucket name
    S3_VECTORS_INDEX_NAME       S3 Vectors index name
    SUPABASE_URL                Supabase project URL
    SUPABASE_SERVICE_ROLE_KEY   Supabase service role key
    AWS_REGION                  AWS region
    BEDROCK_EMBEDDING_MODEL     Bedrock embedding model ID
    AWS_PROFILE                 (optional) named AWS profile for local development
"""

from __future__ import annotations

import json
import logging
import os
import time
from uuid import uuid4

import boto3

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BUCKET = os.environ["BUCKET"]
CONTENT_KEY = os.environ["CONTENT_KEY"]
S3_VECTORS_BUCKET = os.environ["S3_VECTORS_BUCKET"]
S3_VECTORS_INDEX_NAME = os.environ.get("S3_VECTORS_INDEX_NAME", "agentscribe")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
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


def _lookup_user_id(site: str) -> str | None:
    """Query user_site table via Supabase PostgREST."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    import urllib.request

    url = f"{SUPABASE_URL}/rest/v1/user_site?site_name=eq.{site}&select=user_uuid&limit=1"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read())
            return data[0]["user_uuid"] if data else None
    except Exception as exc:
        log.warning("Failed to look up user_id for site %s: %s", site, exc)
        return None


def main() -> None:
    start = time.time()

    _session = boto3.Session(profile_name=AWS_PROFILE or None)
    s3 = _session.client("s3", region_name=AWS_REGION)
    bedrock = _session.client("bedrock-runtime", region_name=AWS_REGION)
    s3vectors = _session.client("s3vectors", region_name=AWS_REGION)

    # a. Fetch content.md
    log.info("Fetching %s from %s", CONTENT_KEY, BUCKET)
    content = s3.get_object(Bucket=BUCKET, Key=CONTENT_KEY)["Body"].read().decode("utf-8")

    # b. Extract site name
    site = CONTENT_KEY.split("/")[0]

    # c. Look up user_id
    user_id = _lookup_user_id(site) or "unknown"
    log.info("Site: %s  user_id: %s", site, user_id)

    # d. Delete existing chunks
    chunks_prefix = f"{site}/.chunks/"
    paginator = s3.get_paginator("list_objects_v2")
    existing_chunk_keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=chunks_prefix):
        for obj in page.get("Contents", []):
            existing_chunk_keys.append({"Key": obj["Key"]})
    if existing_chunk_keys:
        s3.delete_objects(Bucket=BUCKET, Delete={"Objects": existing_chunk_keys, "Quiet": True})
        log.info("Deleted %d existing chunks", len(existing_chunk_keys))

    # e. Delete existing vectors for this path
    try:
        existing_vectors = s3vectors.list_vectors(
            vectorBucketName=S3_VECTORS_BUCKET,
            indexName=S3_VECTORS_INDEX_NAME,
            filter={"path": CONTENT_KEY},
        ).get("vectors", [])
        if existing_vectors:
            vector_ids = [v["key"] for v in existing_vectors]
            # Delete in batches of 100
            for i in range(0, len(vector_ids), 100):
                s3vectors.delete_vectors(
                    vectorBucketName=S3_VECTORS_BUCKET,
                    indexName=S3_VECTORS_INDEX_NAME,
                    keys=vector_ids[i:i + 100],
                )
            log.info("Deleted %d existing vectors", len(vector_ids))
    except Exception as exc:
        log.warning("Failed to delete existing vectors (continuing): %s", exc)

    # f. Chunk the markdown
    from langchain_text_splitters import MarkdownHeaderTextSplitter

    headers = [("#", "H1"), ("##", "H2"), ("###", "H3")]
    splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers)
    raw_chunks = splitter.split_text(content)
    chunks = [c for c in raw_chunks if c.page_content.strip()]
    log.info("Produced %d chunks from %s", len(chunks), CONTENT_KEY)

    if not chunks:
        log.info("No content to index. Done.")
        return

    # g. Embed and store each chunk
    stored = 0
    for chunk in chunks:
        chunk_id = str(uuid4())

        # Write chunk text to S3
        chunk_data = {
            "text": chunk.page_content,
            "metadata": chunk.metadata,
            "source": CONTENT_KEY,
        }
        s3.put_object(
            Bucket=BUCKET,
            Key=f"{site}/.chunks/{chunk_id}",
            Body=json.dumps(chunk_data).encode("utf-8"),
            ContentType="application/json",
        )

        # Create embedding
        try:
            embedding = _embed_with_retry(bedrock, chunk.page_content)
        except Exception as exc:
            log.error("Failed to embed chunk %s: %s — skipping", chunk_id, exc)
            continue

        # Store in S3 Vectors
        try:
            s3vectors.put_vectors(
                vectorBucketName=S3_VECTORS_BUCKET,
                indexName=S3_VECTORS_INDEX_NAME,
                vectors=[
                    {
                        "key": chunk_id,
                        "data": {"float32": embedding},
                        "metadata": {
                            "user_id": user_id,
                            "path": CONTENT_KEY,
                        },
                    }
                ],
            )
            stored += 1
        except Exception as exc:
            log.error("Failed to store vector for chunk %s: %s — skipping", chunk_id, exc)

    elapsed = time.time() - start
    log.info(
        "Indexing complete: site=%s path=%s chunks=%d stored=%d elapsed=%.1fs",
        site, CONTENT_KEY, len(chunks), stored, elapsed,
    )


if __name__ == "__main__":
    main()
