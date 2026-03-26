"""Asset upload and serving endpoints for YoloScribe.

POST /upload   — owner-only; returns a pre-signed S3 PUT URL for direct browser upload.
GET  /asset    — serves image assets from S3 with page visibility access control.
               (Video/audio assets are served via CloudFront signed cookies — PR2.)
GET  /assets   — lists asset keys under a given page path; owner-only.
"""

import logging

from fastapi import APIRouter, HTTPException, Security
from fastapi.responses import Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials

from auth import decode_jwt, get_site_for_user, require_site_owner, _bearer
from config import S3_BUCKET, CLOUDFRONT_DOMAIN, s3
from s3_helpers import (
    ASSET_MAX_BYTES,
    is_safe_asset_path,
    asset_mime_type,
    asset_media_category,
    asset_page_path,
)
from settings_cache import get_page_settings

log = logging.getLogger(__name__)

router = APIRouter()

# Pre-signed PUT URL expiry in seconds (15 minutes is generous for large uploads).
_PRESIGN_EXPIRY = 900


@router.post(
    "/upload",
    tags=["assets"],
    summary="Request a pre-signed S3 PUT URL for asset upload",
    description=(
        "Owner-only. Validates the asset path and extension, then returns a "
        "short-lived pre-signed S3 PUT URL and the public asset URL. "
        "The browser uploads directly to S3 — the backend never handles the file bytes."
    ),
)
async def upload_asset(
    site: str,
    path: str,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> dict:
    if not is_safe_asset_path(path):
        raise HTTPException(status_code=400, detail="Invalid asset path or extension")

    claims = decode_jwt(credentials)
    user_site = get_site_for_user(claims.user_id)
    require_site_owner(site, user_site)

    mime = asset_mime_type(path)
    category = asset_media_category(mime)
    max_bytes = ASSET_MAX_BYTES[category]

    s3_key = f"{site}/{path}"

    presigned_url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": S3_BUCKET,
            "Key": s3_key,
            "ContentType": mime,
        },
        ExpiresIn=_PRESIGN_EXPIRY,
        HttpMethod="PUT",
    )

    # Build the public asset URL.  In production this goes through CloudFront;
    # in local dev it hits MinIO directly via the /asset proxy endpoint.
    if CLOUDFRONT_DOMAIN:
        asset_url = f"https://{CLOUDFRONT_DOMAIN}/{site}/{path}"
    else:
        asset_url = f"/api/asset?site={site}&path={path}"

    return {
        "upload_url": presigned_url,
        "asset_url": asset_url,
        "content_type": mime,
        "max_bytes": max_bytes,
    }


@router.get(
    "/asset",
    tags=["assets"],
    summary="Serve an asset from S3",
    description=(
        "Proxies the asset bytes from S3 to the browser. "
        "Page visibility rules apply — private/shared pages require a JWT. "
        "Only image assets are served here; video and audio are served via "
        "CloudFront signed cookies (see GET /media-auth, implemented in PR2)."
    ),
)
async def get_asset(
    site: str,
    path: str,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> Response:
    if not is_safe_asset_path(path):
        raise HTTPException(status_code=400, detail="Invalid asset path or extension")

    mime = asset_mime_type(path)
    category = asset_media_category(mime)

    # In production, video/audio are served via CloudFront signed cookies.
    # Block proxy serving of non-image assets so the endpoint isn't misused.
    if category != "image":
        raise HTTPException(
            status_code=400,
            detail="Video and audio assets must be accessed via CloudFront (see GET /media-auth)",
        )

    # Resolve page visibility for the page this asset belongs to.
    page = asset_page_path(path)
    settings = get_page_settings(site, page)
    visibility = settings.get("visibility", "private")

    if visibility != "public":
        # Auth required for private / shared pages.
        if credentials is None:
            raise HTTPException(status_code=403, detail="Authentication required")
        claims = decode_jwt(credentials)
        user_site = get_site_for_user(claims.user_id)

        if user_site != site:
            if visibility == "shared":
                shared_with = settings.get("shared_with", [])
                if not any(u.get("email") == claims.email for u in shared_with):
                    raise HTTPException(status_code=403, detail="Access denied")
            else:
                raise HTTPException(status_code=403, detail="Access denied")

    s3_key = f"{site}/{path}"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
    except s3.exceptions.NoSuchKey:
        raise HTTPException(status_code=404, detail="Asset not found")

    body = obj["Body"].read()
    return Response(content=body, media_type=mime)


@router.get(
    "/assets",
    tags=["assets"],
    summary="List assets for a page",
    description="Owner-only. Returns the list of asset keys stored under the given page path.",
)
async def list_assets(
    site: str,
    page_path: str = "",
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> dict:
    claims = decode_jwt(credentials)
    user_site = get_site_for_user(claims.user_id)
    require_site_owner(site, user_site)

    prefix = f"{site}/{page_path + '/' if page_path else ''}assets/"
    paginator = s3.get_paginator("list_objects_v2")

    keys: list[str] = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            # Strip the site prefix so the caller gets paths relative to the site.
            keys.append(obj["Key"].removeprefix(f"{site}/"))

    return {"assets": keys}
