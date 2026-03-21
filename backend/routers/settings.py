import datetime
import json

from fastapi import APIRouter, Depends, HTTPException
from starlette.requests import Request

from auth import JWTClaims, get_jwt_claims, get_user_context, require_site_owner
from rate_limit import limiter
from config import S3_BUCKET, s3
from models import AccessRequest, PageSettings
from s3_helpers import get_content, put_content
from settings_cache import get_page_settings, invalidate_settings_cache, page_path_from_file_path

router = APIRouter()


@router.get("/settings", tags=["settings"], summary="Get page access-control settings")
async def get_settings(
    site: str = "default",
    path: str = "content.md",
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    """Return access-control settings for a page (site owner only)."""
    user_id, user_site = ctx
    require_site_owner(site, user_site)
    page_path = page_path_from_file_path(path)
    return get_page_settings(site, page_path)


@router.put("/settings", tags=["settings"], summary="Update page access-control settings")
async def put_settings(
    settings: PageSettings,
    site: str = "default",
    path: str = "content.md",
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict[str, str]:
    """Update access-control settings for a page (site owner only)."""
    user_id, user_site = ctx
    require_site_owner(site, user_site)

    valid_visibilities = {"public", "private", "shared"}
    if settings.visibility not in valid_visibilities:
        raise HTTPException(status_code=400, detail=f"visibility must be one of: {', '.join(sorted(valid_visibilities))}")
    valid_accesses = {"view", "write"}
    for su in settings.shared_with:
        if su.access not in valid_accesses:
            raise HTTPException(status_code=400, detail="shared_with access must be 'view' or 'write'")

    page_path = page_path_from_file_path(path)
    s3_path = "settings.json" if not page_path else f"{page_path}/settings.json"
    put_content(site, s3_path, json.dumps(settings.model_dump()))
    invalidate_settings_cache(site, page_path)
    return {"status": "saved"}


@router.post("/request-access", tags=["access"], summary="Request access to a page")
@limiter.limit("5/hour")
async def request_access(
    request: Request,
    req: AccessRequest,
    claims: JWTClaims = Depends(get_jwt_claims),
) -> dict[str, str]:
    """Append an access-request notification to the site owner's notifications file."""
    if not req.site or not req.path:
        raise HTTPException(status_code=400, detail="site and path are required")

    page_path = page_path_from_file_path(req.path)
    content_key = f"{req.site}/{req.path}"
    check = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=content_key, MaxKeys=1)
    if check.get("KeyCount", 0) == 0:
        raise HTTPException(status_code=404, detail="Page not found")

    settings = get_page_settings(req.site, page_path)
    if claims.email:
        already_shared = any(
            u.get("email") == claims.email for u in settings.get("shared_with", [])
        )
        if already_shared:
            raise HTTPException(status_code=409, detail="You already have access to this page")

    ts = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    requester = claims.email or claims.user_id
    entry = f"- [{ts}] **{requester}** requested access to `{req.path}`\n"

    existing = get_content(req.site, ".user/notifications.md")
    put_content(req.site, ".user/notifications.md", existing + entry)
    return {"status": "ok"}
