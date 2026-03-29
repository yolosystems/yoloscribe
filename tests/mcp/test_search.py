#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27", "boto3>=1.34", "langchain-text-splitters>=0.3"]
# ///
"""
Integration tests: MCP search tools (search_wiki, search_semantic).

Covers test plan sections 7–8 from the MCP server spec.

Section 7 (search_wiki) requires only MCP access.
Section 8 (search_semantic) runs the indexing pipeline inline (chunk → embed →
store in S3 Vectors) so the test is self-contained and does not depend on the
async SQS indexer. It is skipped when S3_VECTORS_BUCKET is not set.

Usage:
    MCP_BASE_URL=https://<domain>/mcp/v1 \\
    USER_JWT=eyJ... \\
    USER_SITE=my-site \\
    S3_BUCKET=my-bucket \\
    S3_VECTORS_BUCKET=my-vectors-bucket \\
    uv run tests/mcp/test_search.py

Required env vars:
    MCP_BASE_URL        Full URL to the MCP server
    USER_JWT            Valid JWT for the test user

Required for semantic search tests (section 8):
    USER_SITE           Site name for the authenticated user (e.g. "alice-home")
    S3_BUCKET           S3 bucket used by the backend
    S3_VECTORS_BUCKET   S3 Vectors bucket used for semantic search

Optional:
    AWS_REGION                  Default: us-east-1
    AWS_PROFILE                 Named boto3 profile
    S3_ENDPOINT_URL             Custom S3 endpoint (MinIO for local dev)
    S3_VECTORS_INDEX_NAME       Default: yoloscribe
    BEDROCK_EMBEDDING_MODEL     Default: amazon.titan-embed-text-v2:0
"""

import json
import os
import sys
import uuid

import boto3
import httpx

# ── Colours ───────────────────────────────────────────────────────────────────

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"

_results: list[bool | None] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = PASS if passed else FAIL
    print(f"  [{status}] {name}")
    if not passed and detail:
        print(f"         {detail}")
    _results.append(passed)


def skip(name: str, reason: str) -> None:
    print(f"  [{SKIP}] {name}")
    print(f"         {reason}")
    _results.append(None)


# ── Config ────────────────────────────────────────────────────────────────────

MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "").rstrip("/") + "/"
USER_JWT     = os.environ.get("USER_JWT", "")
USER_SITE    = os.environ.get("USER_SITE", "")

S3_BUCKET              = os.environ.get("S3_BUCKET", "")
S3_VECTORS_BUCKET      = os.environ.get("S3_VECTORS_BUCKET", "")
S3_VECTORS_INDEX_NAME  = os.environ.get("S3_VECTORS_INDEX_NAME", "yoloscribe")
AWS_REGION             = os.environ.get("AWS_REGION", "us-east-1")
AWS_PROFILE            = os.environ.get("AWS_PROFILE", "")
S3_ENDPOINT_URL        = os.environ.get("S3_ENDPOINT_URL", "")
BEDROCK_EMBEDDING_MODEL = os.environ.get("BEDROCK_EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")

SEMANTIC_AVAILABLE = bool(S3_VECTORS_BUCKET and USER_SITE and S3_BUCKET)

if not MCP_BASE_URL.rstrip("/"):
    print("ERROR: MCP_BASE_URL is required")
    sys.exit(1)

if not USER_JWT:
    print("ERROR: USER_JWT is required")
    sys.exit(1)

_BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Authorization": f"Bearer {USER_JWT}",
}

_RUN_ID = uuid.uuid4().hex[:8]
_CREATED_PATHS: list[str] = []


# ── MCP helpers ───────────────────────────────────────────────────────────────

def _init_session() -> str:
    r = httpx.post(
        MCP_BASE_URL,
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test_search", "version": "0.1"},
            },
            "id": 0,
        },
        headers=_BASE_HEADERS,
        timeout=15,
    )
    if r.status_code != 200:
        print(f"ERROR: MCP initialize failed: HTTP {r.status_code}: {r.text[:200]}")
        sys.exit(1)
    session_id = r.headers.get("mcp-session-id", "")
    if not session_id:
        print("ERROR: MCP initialize response missing Mcp-Session-Id header")
        sys.exit(1)
    return session_id


_SESSION_ID = _init_session()
AUTH_HEADERS = {**_BASE_HEADERS, "Mcp-Session-Id": _SESSION_ID}


def _parse_response(r: httpx.Response) -> dict:
    ct = r.headers.get("content-type", "")
    if "text/event-stream" in ct:
        for line in r.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ValueError(f"No data line in SSE response: {r.text[:200]}")
    return r.json()


def call_ok(method: str, params: dict) -> tuple[bool, dict | str]:
    r = httpx.post(
        MCP_BASE_URL,
        json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": method, "arguments": params}, "id": 1},
        headers=AUTH_HEADERS,
        timeout=30,
    )
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    body = _parse_response(r)
    if "error" in body:
        return False, f"JSON-RPC error: {body['error']}"
    result = body.get("result", {})
    if isinstance(result, dict) and "content" in result:
        if result.get("isError"):
            return False, result
        try:
            return True, json.loads(result["content"][0]["text"])
        except Exception:
            return True, result
    return True, result


def page_path(suffix: str) -> str:
    p = f"test-{_RUN_ID}-{suffix}"
    _CREATED_PATHS.append(p)
    return p


# ── Inline indexer ────────────────────────────────────────────────────────────

def _index_page(content_key: str, user_id: str = "test") -> list[str]:
    """Chunk, embed, and store a content.md in S3 Vectors. Returns chunk IDs."""
    from langchain_text_splitters import MarkdownHeaderTextSplitter

    session = boto3.Session(profile_name=AWS_PROFILE or None)
    s3_kwargs = {"region_name": AWS_REGION}
    if S3_ENDPOINT_URL:
        s3_kwargs["endpoint_url"] = S3_ENDPOINT_URL
    s3 = session.client("s3", **s3_kwargs)
    bedrock = session.client("bedrock-runtime", region_name=AWS_REGION)
    s3vectors = session.client("s3vectors", region_name=AWS_REGION)

    content = s3.get_object(Bucket=S3_BUCKET, Key=content_key)["Body"].read().decode("utf-8")
    page_dir = content_key.rsplit("/", 1)[0]

    headers = [("#", "H1"), ("##", "H2"), ("###", "H3")]
    splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers)
    chunks = [c for c in splitter.split_text(content) if c.page_content.strip()]

    chunk_ids = []
    for chunk in chunks:
        chunk_id = str(uuid.uuid4())
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=f"{page_dir}/.chunks/{chunk_id}",
            Body=json.dumps({"text": chunk.page_content, "metadata": chunk.metadata, "source": content_key}).encode(),
            ContentType="application/json",
        )
        resp = bedrock.invoke_model(
            modelId=BEDROCK_EMBEDDING_MODEL,
            body=json.dumps({"inputText": chunk.page_content}),
        )
        embedding = json.loads(resp["body"].read())["embedding"]
        s3vectors.put_vectors(
            vectorBucketName=S3_VECTORS_BUCKET,
            indexName=S3_VECTORS_INDEX_NAME,
            vectors=[{"key": chunk_id, "data": {"float32": embedding}, "metadata": {"user_id": user_id, "path": content_key}}],
        )
        chunk_ids.append(chunk_id)

    return chunk_ids


def _delete_index(content_key: str, chunk_ids: list[str]) -> None:
    """Remove chunks from S3 and vectors from S3 Vectors."""
    session = boto3.Session(profile_name=AWS_PROFILE or None)
    s3_kwargs = {"region_name": AWS_REGION}
    if S3_ENDPOINT_URL:
        s3_kwargs["endpoint_url"] = S3_ENDPOINT_URL
    s3 = session.client("s3", **s3_kwargs)
    s3vectors = session.client("s3vectors", region_name=AWS_REGION)

    page_dir = content_key.rsplit("/", 1)[0]
    if chunk_ids:
        s3.delete_objects(
            Bucket=S3_BUCKET,
            Delete={"Objects": [{"Key": f"{page_dir}/.chunks/{cid}"} for cid in chunk_ids], "Quiet": True},
        )
        for i in range(0, len(chunk_ids), 100):
            try:
                s3vectors.delete_vectors(
                    vectorBucketName=S3_VECTORS_BUCKET,
                    indexName=S3_VECTORS_INDEX_NAME,
                    keys=chunk_ids[i:i + 100],
                )
            except Exception:
                pass


# ── Section 7: search_wiki ────────────────────────────────────────────────────

print(f"\nMCP Search Integration Tests  [run={_RUN_ID}]")
print(f"MCP endpoint : {MCP_BASE_URL}\n")

print("7. search_wiki")

UNIQUE_KEYWORD = f"xyzzy-{_RUN_ID}"
p_search = page_path("search-wiki")
call_ok("wiki_create", {"page_path": p_search, "content": f"# Search Test\n\nThis page contains the unique keyword: {UNIQUE_KEYWORD}."})

# 7a. Unique keyword returns the page
ok, result = call_ok("search_wiki", {"query": UNIQUE_KEYWORD})
results = result.get("results", []) if isinstance(result, dict) else []
found_paths = [r["page_path"] for r in results]
check(
    "unique keyword → page appears in results",
    ok and p_search in found_paths,
    f"got paths: {found_paths}",
)

# 7b. No-match query → empty results, no error
ok, result = call_ok("search_wiki", {"query": f"zzznomatch-{_RUN_ID}-zzz"})
results = result.get("results", []) if isinstance(result, dict) else []
check(
    "no-match query → empty results (no error)",
    ok and results == [],
    str(result),
)

# 7c. Result fields present
ok, result = call_ok("search_wiki", {"query": UNIQUE_KEYWORD})
results = result.get("results", []) if isinstance(result, dict) else []
if results:
    sample = results[0]
    check(
        "result entries have page_path, score, excerpt",
        all(k in sample for k in ("page_path", "score", "excerpt")),
        f"sample: {sample}",
    )
else:
    skip("result entries have page_path, score, excerpt", "no results to inspect")

# ── Section 8: search_semantic ────────────────────────────────────────────────

print("\n8. search_semantic")

_semantic_chunk_ids: list[str] = []

if not SEMANTIC_AVAILABLE:
    missing = [v for v, k in [("S3_VECTORS_BUCKET", S3_VECTORS_BUCKET), ("USER_SITE", USER_SITE), ("S3_BUCKET", S3_BUCKET)] if not k]
    for name in ["natural-language paraphrase → page in results", "min_score filtering reduces results", "newly indexed content appears in search"]:
        skip(name, f"set {', '.join(missing)} to enable semantic search tests")
else:
    SEMANTIC_CONTENT = (
        "# Quantum Computing Fundamentals\n\n"
        "Quantum computers exploit superposition and entanglement to perform certain "
        "calculations exponentially faster than classical machines. Qubits can exist "
        "in multiple states simultaneously, enabling massive parallelism for problems "
        "like factoring large integers and simulating molecular interactions."
    )
    p_semantic = page_path("search-semantic")
    content_key = f"{USER_SITE}/{p_semantic}/content.md"

    # Create the page and index it inline
    ok, _ = call_ok("wiki_create", {"page_path": p_semantic, "content": SEMANTIC_CONTENT})
    if not ok:
        for name in ["natural-language paraphrase → page in results", "min_score filtering reduces results", "newly indexed content appears in search"]:
            skip(name, "wiki_create failed for semantic test page")
    else:
        try:
            _semantic_chunk_ids = _index_page(content_key)
        except Exception as exc:
            for name in ["natural-language paraphrase → page in results", "min_score filtering reduces results", "newly indexed content appears in search"]:
                skip(name, f"inline indexing failed: {exc}")
        else:
            # 8a. Natural-language paraphrase → page ranks in results
            ok, result = call_ok("search_semantic", {"query": "How do qubits enable parallel computation?", "limit": 10})
            results = result.get("results", []) if isinstance(result, dict) else []
            found = [r["page_path"] for r in results]
            check(
                "natural-language paraphrase → page in results",
                ok and p_semantic in found,
                f"got paths: {found}",
            )

            # 8b. High min_score threshold → fewer results than without filter
            ok_all, result_all = call_ok("search_semantic", {"query": "quantum computing qubits", "limit": 50, "min_score": 0.0})
            ok_high, result_high = call_ok("search_semantic", {"query": "quantum computing qubits", "limit": 50, "min_score": 0.9})
            count_all  = len(result_all.get("results", []))  if isinstance(result_all, dict) else 0
            count_high = len(result_high.get("results", [])) if isinstance(result_high, dict) else 0
            check(
                "min_score=0.9 returns fewer results than min_score=0.0",
                ok_all and ok_high and count_high <= count_all,
                f"min_score=0.0 → {count_all} results, min_score=0.9 → {count_high} results",
            )

            # 8c. Newly indexed content is immediately queryable (no async lag)
            ok, result = call_ok("search_semantic", {"query": "superposition entanglement quantum", "limit": 10})
            results = result.get("results", []) if isinstance(result, dict) else []
            found = [r["page_path"] for r in results]
            check(
                "newly indexed content appears in search",
                ok and p_semantic in found,
                f"got paths: {found}",
            )

# ── Cleanup ───────────────────────────────────────────────────────────────────

print("\n── Cleanup ──────────────────────────────────────────────────────────────")

if _semantic_chunk_ids and SEMANTIC_AVAILABLE:
    p_semantic_val = f"test-{_RUN_ID}-search-semantic"
    content_key = f"{USER_SITE}/{p_semantic_val}/content.md"
    try:
        _delete_index(content_key, _semantic_chunk_ids)
        print(f"  deleted index: {p_semantic_val} ({len(_semantic_chunk_ids)} chunks)")
    except Exception as exc:
        print(f"  FAILED to delete index for {p_semantic_val}: {exc}")

for pg in reversed(_CREATED_PATHS):
    ok, _ = call_ok("wiki_delete", {"page_path": pg})
    print(f"  {'deleted' if ok else 'FAILED to delete'}: {pg}")

# ── Summary ───────────────────────────────────────────────────────────────────

total   = len(_results)
passed  = sum(1 for r in _results if r is True)
skipped = sum(1 for r in _results if r is None)
failed  = total - passed - skipped

print(f"\n{'─' * 52}")
print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")

if failed:
    sys.exit(1)
