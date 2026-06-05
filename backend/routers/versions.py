"""Page version history via S3 object versioning."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Security
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials

from auth import decode_jwt, get_site_for_user, get_user_context, require_site_owner, _bearer
from config import S3_BUCKET, s3
from queue_helpers import enqueue_index_job, enqueue_on_write_agents
from s3_storage import storage
from yoloscribe_io import WikiPageMarkdownFile

router = APIRouter()
log = logging.getLogger(__name__)

_MAX_VERSIONS = 50


def _content_key(site: str, page_path: str) -> str:
    return f"{site}/{page_path}/content.md" if page_path else f"{site}/content.md"


@router.get(
    "/versions",
    tags=["content"],
    summary="List page version history",
    description=(
        "Return the version history of a content page from S3 object versioning. "
        "Returns an empty list if versioning is not enabled or the page has no history. "
        "Requires site ownership."
    ),
)
async def list_versions_route(
    site: str = "default",
    page_path: str = "",
    limit: int = 20,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> JSONResponse:
    _, user_site = get_user_context(credentials)
    require_site_owner(site, user_site)

    key = _content_key(site, page_path)
    limit = max(1, min(limit, _MAX_VERSIONS))

    try:
        resp = s3.list_object_versions(Bucket=S3_BUCKET, Prefix=key)
    except Exception as exc:
        log.warning("list_object_versions failed for %s: %s", key, exc)
        return JSONResponse({"versions": [], "page_path": page_path})

    versions = []
    for v in resp.get("Versions", []):
        if v["Key"] != key:
            continue
        versions.append({
            "version_id": v["VersionId"],
            "last_modified": v["LastModified"].isoformat(),
            "size_bytes": v["Size"],
            "is_latest": v["IsLatest"],
        })
        if len(versions) >= limit:
            break

    return JSONResponse({"versions": versions, "page_path": page_path})


@router.get(
    "/version",
    tags=["content"],
    summary="Get content of a specific page version",
    description="Fetch the raw markdown content of a specific S3 version. Requires site ownership.",
)
async def get_version_route(
    site: str = "default",
    page_path: str = "",
    version_id: str = "",
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> Response:
    _, user_site = get_user_context(credentials)
    require_site_owner(site, user_site)

    if not version_id:
        raise HTTPException(status_code=400, detail="version_id is required")

    key = _content_key(site, page_path)
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=key, VersionId=version_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Version not found")

    content = resp["Body"].read().decode("utf-8")
    return Response(content=content, media_type="text/plain; charset=utf-8")


@router.post(
    "/restore",
    tags=["content"],
    summary="Restore a prior version as current",
    description=(
        "Write a prior S3 version's content back as a new PUT, preserving full version history. "
        "Triggers re-indexing and on_write agents. Requires site ownership."
    ),
)
async def restore_version_route(
    site: str = "default",
    page_path: str = "",
    version_id: str = "",
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> JSONResponse:
    claims = decode_jwt(credentials)
    user_site = get_site_for_user(claims.user_id)
    require_site_owner(site, user_site)

    if not version_id:
        raise HTTPException(status_code=400, detail="version_id is required")

    key = _content_key(site, page_path)
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=key, VersionId=version_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Version not found")

    content = resp["Body"].read().decode("utf-8")
    wiki = WikiPageMarkdownFile(site=site, page_path=page_path, storage=storage)
    wiki.write(content, user_id=claims.user_id)
    enqueue_index_job(wiki.key, claims.user_id)
    enqueue_on_write_agents(site, wiki.key, claims.user_id)

    return JSONResponse({"status": "restored", "version_id": version_id})
