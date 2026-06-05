"""Hybrid search: SQLite FTS5 (keyword) + S3 Vectors (semantic) + RRF fusion.

Optional Haiku query expansion generates 2-3 query variants before searching,
improving recall on paraphrased or imprecise queries.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

from fts_cache import fts_query, get_db_path

log = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_EMBEDDING_MODEL = os.environ.get("BEDROCK_EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

_RRF_K = 60
_EXPANSION_MODEL = "claude-haiku-4-5-20251001"


# ── RRF ───────────────────────────────────────────────────────────────────────


def _rrf_score(rank: int, k: int = _RRF_K) -> float:
    return 1.0 / (k + rank + 1)


def rrf_fuse(
    keyword_results: list[dict],
    semantic_results: list[dict],
) -> list[dict]:
    """Merge keyword and semantic result lists via Reciprocal Rank Fusion.

    Each result dict must have a "page_path" key. Returns a deduplicated list
    sorted by descending fused score, with both source scores attached.
    """
    scores: dict[str, float] = {}
    kw_excerpts: dict[str, str] = {}
    sem_previews: dict[str, str] = {}

    for rank, r in enumerate(keyword_results):
        pp = r["page_path"]
        scores[pp] = scores.get(pp, 0.0) + _rrf_score(rank)
        if pp not in kw_excerpts:
            kw_excerpts[pp] = r.get("excerpt", "")

    for rank, r in enumerate(semantic_results):
        pp = r["page_path"]
        scores[pp] = scores.get(pp, 0.0) + _rrf_score(rank)
        if pp not in sem_previews:
            sem_previews[pp] = r.get("content_preview", "")

    ranked = sorted(scores, key=lambda p: scores[p], reverse=True)
    return [
        {
            "page_path": pp,
            "score": scores[pp],
            "excerpt": kw_excerpts.get(pp) or sem_previews.get(pp, ""),
        }
        for pp in ranked
    ]


# ── Query expansion ───────────────────────────────────────────────────────────


def expand_query(query: str) -> list[str]:
    """Generate 2-3 alternative phrasings via Claude Haiku.

    Returns the original query plus variants. Falls back to [query] on error.
    """
    if not ANTHROPIC_API_KEY:
        return [query]
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=_EXPANSION_MODEL,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f'Generate 2 alternative search queries for: "{query}". '
                    'Return as a JSON array of strings only, no explanation.'
                ),
            }],
        )
        block = resp.content[0]
        variants = json.loads(block.text if hasattr(block, "text") else "")
        if isinstance(variants, list):
            return [query] + [str(v) for v in variants[:2]]
    except Exception as exc:
        log.debug("Query expansion failed (continuing with original): %s", exc)
    return [query]


# ── Keyword search ────────────────────────────────────────────────────────────


def keyword_search(
    s3,
    bucket: str,
    site: str,
    query: str,
    tags: list[str] | None = None,
    limit: int = 30,
) -> list[dict]:
    """Search the SQLite FTS5 index. Returns [] if index is unavailable."""
    db_path = get_db_path(s3, bucket, site)
    if not db_path:
        return []
    return fts_query(db_path, query, limit=limit, tags=tags)


# ── Semantic search ───────────────────────────────────────────────────────────


def semantic_search(
    s3,
    bucket: str,
    site: str,
    query: str,
    s3vectors_client: Any,
    vectors_bucket: str,
    vectors_index: str,
    tags: list[str] | None = None,
    limit: int = 30,
) -> list[dict]:
    """Embed query and query S3 Vectors. Returns [] if not configured."""
    if not s3vectors_client or not vectors_bucket:
        return []

    try:
        bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
        resp = bedrock.invoke_model(
            modelId=BEDROCK_EMBEDDING_MODEL,
            body=json.dumps({"inputText": query}),
        )
        embedding: list[float] = json.loads(resp["body"].read())["embedding"]
    except Exception as exc:
        log.warning("Bedrock embedding failed: %s", exc)
        return []

    try:
        raw = s3vectors_client.query_vectors(
            vectorBucketName=vectors_bucket,
            indexName=vectors_index,
            queryVector={"float32": embedding},
            topK=min(limit * 3, 100),
            returnMetadata=True,
        )
    except Exception as exc:
        log.warning("S3 Vectors query failed: %s", exc)
        return []

    site_prefix = f"{site}/"
    results: list[dict] = []

    for vec in raw.get("vectors", []):
        metadata = vec.get("metadata", {})
        path = metadata.get("path", "")
        vec_tags: list[str] = metadata.get("tags", [])

        if not path.startswith(site_prefix):
            continue

        # Post-filter by tags if requested
        if tags and not any(t in vec_tags for t in tags):
            continue

        relative = path[len(site_prefix):]
        page_path = "" if relative == "content.md" else relative[:-len("/content.md")]

        # Fetch chunk preview
        preview = ""
        try:
            page_dir = path.rsplit("/", 1)[0]
            chunk_key = f"{page_dir}/.chunks/{vec['key']}"
            chunk_obj = s3.get_object(Bucket=bucket, Key=chunk_key)
            chunk_data = json.loads(chunk_obj["Body"].read())
            preview = chunk_data.get("text", "")[:400]
        except Exception:
            pass

        results.append({
            "page_path": page_path,
            "similarity_score": float(vec.get("score", 0.0)),
            "content_preview": preview,
        })

        if len(results) >= limit:
            break

    return results


# ── Unified hybrid search ─────────────────────────────────────────────────────


def hybrid_search(
    s3,
    bucket: str,
    site: str,
    query: str,
    s3vectors_client: Any = None,
    vectors_bucket: str = "",
    vectors_index: str = "",
    tags: list[str] | None = None,
    limit: int = 20,
    expand: bool = False,
) -> list[dict]:
    """Run the full hybrid pipeline and return RRF-fused results.

    Steps:
      1. (Optional) Haiku query expansion → 2-3 query variants
      2. FTS5 keyword search for each variant
      3. S3 Vectors semantic search for each variant
      4. RRF fusion of all results
    """
    queries = expand_query(query) if expand else [query]

    all_keyword: list[dict] = []
    all_semantic: list[dict] = []
    seen_kw: set[str] = set()
    seen_sem: set[str] = set()

    for q in queries:
        for r in keyword_search(s3, bucket, site, q, tags=tags, limit=limit * 2):
            key = r["page_path"]
            if key not in seen_kw:
                seen_kw.add(key)
                all_keyword.append(r)

        for r in semantic_search(
            s3, bucket, site, q,
            s3vectors_client=s3vectors_client,
            vectors_bucket=vectors_bucket,
            vectors_index=vectors_index,
            tags=tags,
            limit=limit * 2,
        ):
            key = r["page_path"]
            if key not in seen_sem:
                seen_sem.add(key)
                all_semantic.append(r)

    fused = rrf_fuse(all_keyword, all_semantic)
    return fused[:limit]
