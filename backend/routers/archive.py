"""Page archive and empty-archive REST endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from archive_helpers import archive_page, empty_archive
from auth import get_user_context, require_site_owner, _bearer
from config import S3_BUCKET, S3_VECTORS_BUCKET, S3_VECTORS_INDEX_NAME, s3, s3vectors

router = APIRouter()


@router.post(
    "/archive",
    tags=["content"],
    summary="Archive a page and its descendants",
    description=(
        "Archive a page and all its descendants: copies content to `.user/archive/`, "
        "removes all search indexes (FTS5 + S3 Vectors chunks), and deletes the originals. "
        "Cannot be used on the root page. Requires site ownership."
    ),
)
async def archive_page_route(
    site: str = "default",
    page_path: str = "",
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> JSONResponse:
    _, user_site = get_user_context(credentials)
    require_site_owner(site, user_site)

    if not page_path:
        raise HTTPException(status_code=400, detail="Cannot archive the root page")

    try:
        result = archive_page(
            s3=s3,
            bucket=S3_BUCKET,
            site=site,
            page_path=page_path,
            s3vectors_client=s3vectors,
            vectors_bucket=S3_VECTORS_BUCKET,
            vectors_index=S3_VECTORS_INDEX_NAME,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return JSONResponse(result)


@router.post(
    "/empty-archive",
    tags=["content"],
    summary="Permanently delete all archived pages",
    description=(
        "Permanently delete everything under `.user/archive/` for this site. "
        "This is irreversible. Requires site ownership."
    ),
)
async def empty_archive_route(
    site: str = "default",
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> JSONResponse:
    _, user_site = get_user_context(credentials)
    require_site_owner(site, user_site)

    result = empty_archive(s3=s3, bucket=S3_BUCKET, site=site)
    return JSONResponse(result)
