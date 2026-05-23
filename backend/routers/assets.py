"""Asset upload, serving, and media-auth endpoints for YoloScribe.

POST   /upload      — owner-only; returns a pre-signed S3 PUT URL for direct browser upload.
GET    /asset       — serves image assets (and video/audio in LOCAL_MODE) from S3 with
                      page visibility access control.
DELETE /asset       — owner-only; permanently deletes an asset from S3.
GET    /assets      — lists asset keys with metadata under a given page path; owner-only.
GET    /media-auth  — issues CloudFront signed cookies for video/audio playback; no-op in
                      LOCAL_MODE (all assets served through /asset instead).
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Security
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials

import cloudfront_signing
from auth import decode_jwt, get_site_for_user, get_user_context, require_site_owner, _bearer
from config import (
    CLOUDFRONT_COOKIE_DOMAIN,
    CLOUDFRONT_MEDIA_DISTRIBUTION_ID,
    CLOUDFRONT_MEDIA_DOMAIN,
    CLOUDFRONT_SIGNING_KEY_ID,
    LOCAL_MODE,
    S3_BUCKET,
    cloudfront,
    s3,
)
from path_safety import (
    ASSET_MAX_BYTES,
    asset_media_category,
    asset_mime_type,
    asset_page_path,
    is_safe_asset_path,
)
from settings_cache import get_page_settings

log = logging.getLogger(__name__)

router = APIRouter()

# Pre-signed PUT URL expiry in seconds (15 minutes is generous for large uploads).
_PRESIGN_EXPIRY = 900

# Cookie TTL matches the signing TTL in cloudfront_signing.
_COOKIE_TTL = cloudfront_signing.COOKIE_TTL


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
    # in local dev all assets (including video/audio) go through the /asset proxy.
    if not LOCAL_MODE and CLOUDFRONT_MEDIA_DOMAIN and category != "image":
        asset_url = f"https://{CLOUDFRONT_MEDIA_DOMAIN}/{site}/{path}"
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
        "Proxies asset bytes from S3 to the browser with page visibility access control. "
        "Images are always served here. In LOCAL_MODE, video and audio are also served "
        "here (CloudFront is unavailable locally). In production, video and audio are "
        "served directly via CloudFront using signed cookies issued by GET /media-auth."
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
    # Block proxy serving of non-image assets so the endpoint isn't misused in prod.
    if category != "image" and not LOCAL_MODE:
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


@router.delete(
    "/asset",
    tags=["assets"],
    summary="Delete an asset from S3",
    description="Owner-only. Permanently deletes the asset at the given path.",
    status_code=204,
)
async def delete_asset(
    site: str,
    path: str,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> Response:
    if not is_safe_asset_path(path):
        raise HTTPException(status_code=400, detail="Invalid asset path or extension")

    claims = decode_jwt(credentials)
    user_site = get_site_for_user(claims.user_id)
    require_site_owner(site, user_site)

    s3_key = f"{site}/{path}"
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
    except Exception as exc:
        log.error("Failed to delete asset %s: %s", s3_key, exc)
        raise HTTPException(status_code=500, detail="Failed to delete asset")

    if cloudfront and CLOUDFRONT_MEDIA_DISTRIBUTION_ID:
        try:
            cloudfront.create_invalidation(
                DistributionId=CLOUDFRONT_MEDIA_DISTRIBUTION_ID,
                InvalidationBatch={
                    "Paths": {"Quantity": 1, "Items": [f"/{s3_key}"]},
                    "CallerReference": s3_key,
                },
            )
            log.info("Invalidated CloudFront path /%s", s3_key)
        except Exception as exc:
            log.error("CloudFront invalidation failed for /%s: %s", s3_key, exc)

    return Response(status_code=204)


@router.get(
    "/assets",
    tags=["assets"],
    summary="List assets for a page",
    description=(
        "Owner-only. Returns asset metadata (path, size, content_type, last_modified) "
        "for all assets stored under the given page path."
    ),
)
async def list_assets(
    site: str,
    page_path: str = "",
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> dict:
    claims = decode_jwt(credentials)
    user_site = get_site_for_user(claims.user_id)
    require_site_owner(site, user_site)

    page_prefix = f"{site}/{page_path + '/' if page_path else ''}"
    prefixes = [f"{page_prefix}assets/", f"{page_prefix}media/"]
    paginator = s3.get_paginator("list_objects_v2")

    assets: list[dict] = []
    for prefix in prefixes:
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                rel_path = obj["Key"].removeprefix(f"{site}/")
                mime = asset_mime_type(rel_path)
                assets.append(
                    {
                        "path": rel_path,
                        "size": obj.get("Size", 0),
                        "content_type": mime,
                        "last_modified": obj["LastModified"].isoformat() if obj.get("LastModified") else None,
                    }
                )

    return {"assets": assets}


@router.get(
    "/media-auth",
    tags=["assets"],
    summary="Issue CloudFront signed cookies for media playback",
    description=(
        "Issues three CloudFront signed cookies (CloudFront-Policy, "
        "CloudFront-Signature, CloudFront-Key-Pair-Id) scoped to the "
        "authenticated user's site assets prefix, valid for 1 hour. "
        "The browser automatically sends these cookies on subsequent requests "
        "to the CloudFront distribution. "
        "In LOCAL_MODE this endpoint returns 200 with no cookies set — all "
        "assets are served through GET /asset instead."
    ),
)
async def media_auth(
    site: str | None = Query(None),
    page_path: str | None = Query(None),
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> Response:
    # In local dev there is no CloudFront — return 200 so the frontend doesn't
    # need to branch on LOCAL_MODE.
    if LOCAL_MODE:
        return Response(content='{"status":"local_mode"}', media_type="application/json")

    if not CLOUDFRONT_MEDIA_DOMAIN or not CLOUDFRONT_SIGNING_KEY_ID:
        raise HTTPException(
            status_code=503,
            detail="CloudFront media domain or signing key ID is not configured",
        )

    if not cloudfront_signing.is_configured():
        raise HTTPException(
            status_code=503,
            detail="CloudFront signing key is not available",
        )

    if credentials:
        # Authenticated: derive site from JWT.
        _, user_site = get_user_context(credentials)
        if not user_site:
            raise HTTPException(status_code=403, detail="No site associated with this account")
    else:
        # Unauthenticated: site must be provided; issue cookies only if the
        # requested page (or root when page_path is absent) is public.
        if not site:
            raise HTTPException(status_code=401, detail="Authentication required")
        check_path = page_path or ""
        settings = get_page_settings(site, check_path)
        if settings.get("visibility") != "public":
            raise HTTPException(status_code=401, detail="Authentication required")
        user_site = site

    try:
        cookies = cloudfront_signing.sign_media_cookies(
            cloudfront_domain=CLOUDFRONT_MEDIA_DOMAIN,
            site=user_site,
            key_pair_id=CLOUDFRONT_SIGNING_KEY_ID,
        )
    except Exception as exc:
        log.error("Failed to sign CloudFront cookies for site %s: %s", user_site, exc)
        raise HTTPException(status_code=500, detail="Failed to generate media access cookies")

    resp = Response(content='{"status":"ok"}', media_type="application/json")

    # Cookies must be sent to the CloudFront domain so the browser attaches them
    # to CloudFront requests.  SameSite=None; Secure is required for cross-origin
    # cookie delivery (the API is on a different origin from CloudFront).
    cookie_opts = {
        "domain": CLOUDFRONT_COOKIE_DOMAIN,
        "max_age": _COOKIE_TTL,
        "httponly": True,
        "secure": True,
        "samesite": "none",
        "path": f"/{user_site}/",
    }

    for name, value in cookies.items():
        resp.set_cookie(name, value, **cookie_opts)

    return resp
