"""SearchBackend abstraction for semantic wiki search in the agent runner.

The backend is injectable so the Bedrock + S3 Vectors implementation can later
be replaced with a more advanced hybrid search module without touching agent
code.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_DEFAULT_TOP_K = 20
_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"


@dataclass
class SearchResult:
    page_path: str
    excerpt: str
    score: float = 0.0


class SearchBackend(ABC):
    @abstractmethod
    def search(self, query: str, site: str, limit: int = 10) -> list[SearchResult]:
        """Return up to `limit` results for `query` scoped to `site`."""
        ...


class NullSearchBackend(SearchBackend):
    """No-op backend for agents that do not require search."""

    def search(self, query: str, site: str, limit: int = 10) -> list[SearchResult]:
        return []


class BedrockS3VectorsSearchBackend(SearchBackend):
    """Semantic search using Bedrock embeddings and S3 Vectors.

    Results are filtered to the requested site by comparing the site prefix
    of the chunk's content key against the requested site.

    Requires:
        - bedrock_client: boto3 bedrock-runtime client
        - s3vectors_client: boto3 s3vectors client
        - s3_client: boto3 s3 client (for fetching chunk text)
        - bucket: main content S3 bucket
        - vectors_bucket: S3 Vectors bucket name
        - index_name: S3 Vectors index name (default: "yoloscribe")
    """

    def __init__(
        self,
        bedrock_client,
        s3vectors_client,
        s3_client,
        bucket: str,
        vectors_bucket: str,
        index_name: str = "yoloscribe",
    ) -> None:
        self._bedrock = bedrock_client
        self._vectors = s3vectors_client
        self._s3 = s3_client
        self._bucket = bucket
        self._vectors_bucket = vectors_bucket
        self._index_name = index_name

    def search(self, query: str, site: str, limit: int = 10) -> list[SearchResult]:
        try:
            embedding = self._embed(query)
        except Exception as exc:
            log.error("Failed to embed search query: %s", exc)
            return []

        try:
            # Request more than limit so site filtering doesn't exhaust results.
            raw = self._query_vectors(embedding, min(_DEFAULT_TOP_K, limit * 3))
        except Exception as exc:
            log.error("Failed to query S3 Vectors: %s", exc)
            return []

        results: list[SearchResult] = []
        for r in raw:
            vector_id = r.get("key", "")
            metadata = r.get("metadata", {})
            content_path = metadata.get("path", "")
            result_site = content_path.split("/")[0] if content_path else ""
            if result_site != site:
                continue
            text = self._fetch_chunk(content_path, vector_id)
            if not text:
                continue
            page_path = _content_key_to_page_path(content_path)
            score = float(r.get("score", 0.0))
            results.append(SearchResult(page_path=page_path, excerpt=text, score=score))
            if len(results) >= limit:
                break

        return results

    def _embed(self, text: str) -> list[float]:
        resp = self._bedrock.invoke_model(
            modelId=_EMBEDDING_MODEL,
            body=json.dumps({"inputText": text}),
        )
        return json.loads(resp["body"].read())["embedding"]

    def _query_vectors(self, embedding: list[float], top_k: int) -> list[dict]:
        resp = self._vectors.query_vectors(
            vectorBucketName=self._vectors_bucket,
            indexName=self._index_name,
            queryVector={"float32": embedding},
            topK=top_k,
            returnMetadata=True,
        )
        return resp.get("vectors", [])

    def _fetch_chunk(self, content_key_path: str, vector_id: str) -> str:
        try:
            page_dir = content_key_path.rsplit("/", 1)[0]
            key = f"{page_dir}/.chunks/{vector_id}"
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
            data = json.loads(obj["Body"].read())
            return data.get("text", "")
        except Exception:
            return ""


def _content_key_to_page_path(content_key: str) -> str:
    """Convert a full content key to a page path (strip site prefix and /content.md).

    e.g. "knuth/projects/yoloscribe/feature-backlog/content.md"
         → "projects/yoloscribe/feature-backlog"
    """
    parts = content_key.split("/")
    inner = parts[1:]  # drop site
    if inner and inner[-1] == "content.md":
        inner = inner[:-1]
    return "/".join(inner)
