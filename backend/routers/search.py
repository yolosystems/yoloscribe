"""Hybrid search REST endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from auth import get_user_context, _bearer
from config import S3_BUCKET, S3_VECTORS_BUCKET, S3_VECTORS_INDEX_NAME, s3, s3vectors
from hybrid_search import hybrid_search

router = APIRouter()


@router.get(
    "/search",
    tags=["content"],
    summary="Hybrid search",
    description=(
        "Search wiki pages using the hybrid pipeline: "
        "SQLite FTS5 keyword search + S3 Vectors semantic search + RRF fusion. "
        "Optionally filter by frontmatter tags. "
        "Pass `expand=true` to use Haiku query expansion for better recall."
    ),
)
async def search_route(
    site: str = "default",
    query: str = "",
    tags: str = "",
    limit: int = 20,
    expand: bool = False,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> JSONResponse:
    _, user_site = get_user_context(credentials)

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    limit = max(1, min(limit, 100))

    results = hybrid_search(
        s3=s3,
        bucket=S3_BUCKET,
        site=site,
        query=query,
        s3vectors_client=s3vectors,
        vectors_bucket=S3_VECTORS_BUCKET,
        vectors_index=S3_VECTORS_INDEX_NAME,
        tags=tag_list,
        limit=limit,
        expand=expand,
    )

    return JSONResponse({"results": results, "total_hits": len(results)})
