"""SearchAgent — semantic search across all indexed wiki content."""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import TYPE_CHECKING

import boto3

if TYPE_CHECKING:
    import mypy_boto3_s3

log = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_EMBEDDING_MODEL = os.environ.get("BEDROCK_EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")
S3_VECTORS_BUCKET = os.environ.get("S3_VECTORS_BUCKET", "")
S3_VECTORS_INDEX_NAME = os.environ.get("S3_VECTORS_INDEX_NAME", "yoloscribe")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

_TOP_K = 10


class SearchAgent:
    """Performs semantic search across all indexed wiki content.

    Steps:
    1. Embed the query via Bedrock
    2. Query S3 Vectors (global, no user filter)
    3. Fetch chunk text from S3 for each result
    4. Generate a ranked markdown summary via Anthropic
    5. Prepend the entry to {user_site}/.user/search.md
    6. Return (summary_reply, navigate_to)
    """

    def __init__(
        self,
        s3: "mypy_boto3_s3.S3Client",
        bucket: str,
        aws_region: str = AWS_REGION,
        bedrock_embedding_model: str = BEDROCK_EMBEDDING_MODEL,
        s3_vectors_bucket: str = S3_VECTORS_BUCKET,
        s3_vectors_index_name: str = S3_VECTORS_INDEX_NAME,
    ) -> None:
        self._s3 = s3
        self._bucket = bucket
        self._aws_region = aws_region
        self._bedrock_embedding_model = bedrock_embedding_model
        self._s3_vectors_bucket = s3_vectors_bucket
        self._s3_vectors_index_name = s3_vectors_index_name

    def run(self, query: str, user_site: str) -> tuple[str, str]:
        """Run search and return (reply, navigate_to).

        navigate_to is always '#/.user/search'.
        """
        from hybrid_search import hybrid_search

        # 1. Run hybrid search
        try:
            fused = hybrid_search(
                s3=self._s3,
                bucket=self._bucket,
                site=user_site,
                query=query,
                s3vectors_client=boto3.client("s3vectors", region_name=self._aws_region) if self._s3_vectors_bucket else None,
                vectors_bucket=self._s3_vectors_bucket,
                vectors_index=self._s3_vectors_index_name,
                limit=_TOP_K,
                expand=False,
            )
        except Exception as exc:
            log.error("Hybrid search failed: %s", exc)
            return f"Search failed: {exc}.", "#/.user/search"

        if not fused:
            summary = f'No results found for "{query}".'
            self._append_search_entry(user_site, query, summary)
            return summary, "#/.user/search"

        # 2. Build chunks list for summary generation (reuse excerpt as text)
        chunks = [
            {"path": f"{user_site}/{r['page_path']}/content.md", "text": r.get("excerpt", "")}
            for r in fused
            if r.get("excerpt")
        ]

        if not chunks:
            summary = f'No readable results found for "{query}".'
            self._append_search_entry(user_site, query, summary)
            return summary, "#/.user/search"

        # 3. Generate markdown summary via Anthropic
        try:
            summary_md = self._generate_summary(query, chunks)
        except Exception as exc:
            log.error("Failed to generate search summary: %s", exc)
            # Fall back to a simple list
            summary_md = self._simple_summary(query, chunks)

        # 5. Write to {user_site}/.user/search.md
        self._append_search_entry(user_site, query, summary_md)

        return summary_md, "#/.user/search"

    # ── internal helpers ─────────────────────────────────────────────────────

    def _embed(self, text: str) -> list[float]:
        bedrock = boto3.client("bedrock-runtime", region_name=self._aws_region)
        resp = bedrock.invoke_model(
            modelId=self._bedrock_embedding_model,
            body=json.dumps({"inputText": text}),
        )
        return json.loads(resp["body"].read())["embedding"]

    def _query_vectors(self, embedding: list[float]) -> list[dict]:
        s3vectors = boto3.client("s3vectors", region_name=self._aws_region)
        resp = s3vectors.query_vectors(
            vectorBucketName=self._s3_vectors_bucket,
            indexName=self._s3_vectors_index_name,
            queryVector={"float32": embedding},
            topK=_TOP_K,
            returnMetadata=True,
        )
        return resp.get("vectors", [])

    def _fetch_chunk(self, content_key_path: str, vector_id: str) -> str:
        """Fetch chunk text from S3.

        content_key_path is the full content_key stored in the vector metadata
        (e.g. "knuth/blog/content.md"). The chunk lives in the .chunks directory
        co-located with that content.md file.
        """
        try:
            page_dir = content_key_path.rsplit("/", 1)[0]
            key = f"{page_dir}/.chunks/{vector_id}"
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
            data = json.loads(obj["Body"].read())
            return data.get("text", "")
        except Exception:
            return ""

    @staticmethod
    def _page_link(s3_path: str) -> str:
        """Convert an S3 content key to a page path (no leading #).

        e.g. "knuth/projects/yoloscribe/feature-backlog/content.md"
             → "projects/yoloscribe/feature-backlog"
        """
        parts = s3_path.split("/")
        # Drop the site prefix (first part) and trailing "content.md"
        inner = parts[1:]
        if inner and inner[-1] == "content.md":
            inner = inner[:-1]
        return "/".join(inner)

    def _generate_summary(self, query: str, chunks: list[dict]) -> str:
        import anthropic

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        chunks_text = "\n\n".join(
            f"Source path: {c['path']}\nPage link: {self._page_link(c['path'])}\nExcerpt:\n{c['text'][:800]}"
            for c in chunks
        )

        prompt = (
            f'The user searched for: "{query}"\n\n'
            f"Here are the top matching content excerpts from the wiki:\n\n"
            f"{chunks_text}\n\n"
            f"Generate a concise ranked markdown list of results. "
            f"For each result include:\n"
            f"- A heading linking to the source page\n"
            f"- A brief excerpt (blockquote)\n\n"
            f"Format each result as:\n"
            f"### [Page title](#page_link)\n"
            f"> excerpt text\n\n"
            f"Replace 'page_link' with the exact 'Page link' value provided for each source. "
            f"Do not add extra '#' symbols — the '#' shown in the format above is sufficient. "
            f"Do not modify or invent URLs. "
            f"Only include results that are genuinely relevant to the query."
        )

        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text

    def _simple_summary(self, query: str, chunks: list[dict]) -> str:
        lines = [f'### Search results for "{query}"\n']
        seen_paths: set[str] = set()
        for c in chunks:
            path = c["path"]
            if path in seen_paths:
                continue
            seen_paths.add(path)
            link = self._page_link(path)
            excerpt = c["text"][:300].replace("\n", " ")
            lines.append(f"### [{link}](#{link})\n> {excerpt}\n")
        return "\n".join(lines)

    def _append_search_entry(self, user_site: str, query: str, summary: str) -> None:
        key = f"{user_site}/.user/search.md"
        now = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        new_entry = f'## Query: "{query}" — {now}\n\n{summary}\n\n---\n\n'

        try:
            existing = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read().decode("utf-8")
        except Exception:
            existing = ""

        combined = new_entry + existing

        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=combined.encode("utf-8"),
                ContentType="text/markdown; charset=utf-8",
            )
        except Exception as exc:
            log.error("Failed to write search.md for site %s: %s", user_site, exc)
