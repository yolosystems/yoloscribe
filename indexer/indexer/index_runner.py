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

from .chunker import chunk_markdown, parse_frontmatter_tags
from .fts_index import delete_agents_for_page, download_or_create, update_page, upload

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

    # Skip .user/ paths — platform/system files should not appear in search results.
    # Ingest content is ephemeral staging; it belongs in wiki pages (which are indexed).
    if "/.user/" in content_key:
        log.info("Skipping .user/ path (not wiki content): %s", content_key)
        return

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
        log.info("No content to index — updating FTS with empty page (agents still indexed).")
        _update_fts_with_agents(s3, bucket, site, page_path, page_dir, [], bedrock, s3vectors, user_id)
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
            metadata: dict = {"user_id": user_id, "path": content_key, "doc_type": "content"}
            if chunk["tags"]:
                metadata["tags"] = chunk["tags"]
            s3vectors.put_vectors(
                vectorBucketName=S3_VECTORS_BUCKET,
                indexName=S3_VECTORS_INDEX_NAME,
                vectors=[{
                    "key": chunk_id,
                    "data": {"float32": embedding},
                    "metadata": metadata,
                }],
            )
            stored += 1
        except Exception as exc:
            log.error("Failed to store vector for chunk %s: %s — skipping", chunk_id, exc)

    # Build / update SQLite FTS5 index and index co-located agents
    _update_fts_with_agents(s3, bucket, site, page_path, page_dir, chunks, bedrock, s3vectors, user_id)

    elapsed = time.time() - start
    log.info(
        "Indexing complete: site=%s page=%r chunks=%d stored=%d elapsed=%.1fs",
        site, page_path, len(chunks), stored, elapsed,
    )


def _update_fts_with_agents(
    s3,
    bucket: str,
    site: str,
    page_path: str,
    page_dir: str,
    chunks: list[dict],
    bedrock,
    s3vectors,
    user_id: str,
) -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    try:
        download_or_create(s3, bucket, site, db_path)
        update_page(db_path, page_path, chunks)
        # Re-index all agents co-located under this page
        _index_page_agents(s3, bucket, site, page_path, page_dir, db_path, bedrock, s3vectors, user_id)
        upload(s3, bucket, site, db_path)
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def _agent_text(content: str, agent_name: str) -> str:
    """Build a searchable text blob from an agent.md file."""
    _, fm, body = _split_agent_md(content)
    parts = []
    if body.strip():
        parts.append(body.strip())
    # Append structured frontmatter fields as searchable text
    for field in ("trigger", "schedule", "skills", "model", "type"):
        val = _fm_field(fm, field)
        if val:
            parts.append(f"{field}: {val}")
    parts.append(f"agent: {agent_name}")
    return "\n".join(parts)


def _split_agent_md(content: str) -> tuple[str, str, str]:
    """Return (raw, frontmatter_block, body) from agent.md content."""
    import re
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if m:
        return content, m.group(1), content[m.end():]
    return content, "", content


def _fm_field(fm: str, field: str) -> str:
    import re
    # Handle list fields (skills): join items
    block = re.search(rf"^{field}:\s*\n((?:[ \t]*-[ \t]+.+\n?)*)", fm, re.MULTILINE)
    if block:
        items = [re.sub(r"^[ \t]*-[ \t]+", "", line).strip()
                 for line in block.group(1).splitlines() if line.strip()]
        return ", ".join(items)
    inline = re.search(rf"^{field}:\s*(.+)", fm, re.MULTILINE)
    if inline:
        return inline.group(1).strip()
    return ""


def _index_page_agents(
    s3,
    bucket: str,
    site: str,
    page_path: str,
    page_dir: str,
    db_path: str,
    bedrock,
    s3vectors,
    user_id: str,
) -> None:
    """Index all agent.md files under {page_dir}/.agents/ into FTS and S3 Vectors."""
    agents_prefix = f"{page_dir}/.agents/"
    try:
        paginator = s3.get_paginator("list_objects_v2")
        agent_keys = []
        for page in paginator.paginate(Bucket=bucket, Prefix=agents_prefix):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith("/agent.md"):
                    agent_keys.append(obj["Key"])
    except Exception as exc:
        log.warning("Failed to list agents for %s: %s", page_dir, exc)
        return

    # Clear existing agent FTS rows for this page before re-adding
    delete_agents_for_page(db_path, page_path)

    # Delete existing agent vectors for this page
    agent_chunks_prefix = f"{page_dir}/.agents/"
    try:
        existing_agent_chunk_keys = []
        existing_agent_vector_ids = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=agent_chunks_prefix):
            for obj in page.get("Contents", []):
                if "/.chunks/" in obj["Key"]:
                    existing_agent_chunk_keys.append({"Key": obj["Key"]})
                    existing_agent_vector_ids.append(obj["Key"].split("/")[-1])
        if existing_agent_chunk_keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": existing_agent_chunk_keys, "Quiet": True})
        if existing_agent_vector_ids and s3vectors and S3_VECTORS_BUCKET:
            for i in range(0, len(existing_agent_vector_ids), 100):
                try:
                    s3vectors.delete_vectors(
                        vectorBucketName=S3_VECTORS_BUCKET,
                        indexName=S3_VECTORS_INDEX_NAME,
                        keys=existing_agent_vector_ids[i:i + 100],
                    )
                except Exception as exc:
                    log.warning("Failed to delete agent vectors: %s", exc)
    except Exception as exc:
        log.warning("Failed to clean up old agent chunks: %s", exc)

    for agent_key in agent_keys:
        # e.g. "knuth/blog/.agents/sync/agent.md" → agent_name = "sync"
        parts = agent_key.split("/.agents/")
        if len(parts) != 2:
            continue
        agent_name = parts[1].replace("/agent.md", "")

        try:
            raw = s3.get_object(Bucket=bucket, Key=agent_key)["Body"].read().decode("utf-8")
        except Exception as exc:
            log.warning("Failed to read agent.md %s: %s", agent_key, exc)
            continue

        text = _agent_text(raw, agent_name)
        if not text.strip():
            continue

        chunk_id = str(uuid4())
        agent_dir = agent_key[:-len("/agent.md")]

        # Store chunk in S3
        chunk_data = {"text": text, "tags": [], "source": agent_key}
        try:
            s3.put_object(
                Bucket=bucket,
                Key=f"{agent_dir}/.chunks/{chunk_id}",
                Body=json.dumps(chunk_data).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception as exc:
            log.warning("Failed to store agent chunk for %s: %s", agent_key, exc)
            continue

        # Embed and store in S3 Vectors
        if s3vectors and S3_VECTORS_BUCKET:
            try:
                embedding = _embed_with_retry(bedrock, text)
                s3vectors.put_vectors(
                    vectorBucketName=S3_VECTORS_BUCKET,
                    indexName=S3_VECTORS_INDEX_NAME,
                    vectors=[{
                        "key": chunk_id,
                        "data": {"float32": embedding},
                        "metadata": {
                            "user_id": user_id,
                            "path": agent_key,
                            "doc_type": "agent",
                            "agent_name": agent_name,
                        },
                    }],
                )
            except Exception as exc:
                log.warning("Failed to embed/store vector for agent %s: %s", agent_key, exc)

        # Update FTS
        update_page(db_path, page_path, [{"text": text, "tags": []}], doc_type="agent", agent_name=agent_name)
        log.info("Indexed agent: %s on page %s", agent_name, page_path or "(root)")


if __name__ == "__main__":
    main()
