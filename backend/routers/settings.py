from fastapi import APIRouter, Depends, HTTPException
from starlette.requests import Request

from auth import JWTClaims, get_jwt_claims, get_user_context, require_site_owner
from rate_limit import limiter
from config import S3_BUCKET, s3
from models import AccessRequest, PageSettings as PageSettingsModel
from notifications import write_notification
from s3_storage import storage
from settings_cache import get_page_settings, invalidate_settings_cache, page_path_from_file_path
from yoloscribe_io import PageSettings, SettingsData, SharedUser

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
    settings: PageSettingsModel,
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
    old = get_page_settings(site, page_path)

    page_settings = PageSettings(site, page_path, storage)
    new_data = SettingsData(
        visibility=settings.visibility,
        shared_with=[SharedUser(email=su.email, access=su.access) for su in settings.shared_with],
    )
    page_settings.save(new_data)
    invalidate_settings_cache(site, page_path)

    _emit_visibility_notifications(site, page_path or "(root)", old, settings.model_dump(), user_id)
    return {"status": "saved"}


def _emit_visibility_notifications(
    site: str,
    page: str,
    old: dict,
    new: dict,
    user_id: str,
) -> None:
    """Emit notifications for visibility and sharing changes (best-effort)."""
    try:
        old_visibility = old.get("visibility", "private")
        new_visibility = new.get("visibility", "private")
        if old_visibility != new_visibility:
            write_notification(
                site,
                "page_visibility_changed",
                {"page": page, "old_visibility": old_visibility, "new_visibility": new_visibility},
                user_id=user_id,
            )

        old_users = {u["email"]: u["access"] for u in old.get("shared_with", [])}
        new_users = {u["email"]: u["access"] for u in new.get("shared_with", [])}

        for email, access in new_users.items():
            if email not in old_users:
                write_notification(
                    site,
                    "page_shared",
                    {"page": page, "shared_with": email, "access": access},
                    user_id=user_id,
                )
            elif old_users[email] != access:
                write_notification(
                    site,
                    "page_access_changed",
                    {"page": page, "user": email, "old_access": old_users[email], "new_access": access},
                    user_id=user_id,
                )

        for email in old_users:
            if email not in new_users:
                write_notification(
                    site,
                    "page_unshared",
                    {"page": page, "removed_user": email},
                    user_id=user_id,
                )
    except Exception:
        pass  # notifications are best-effort; never block a settings save


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

    requester = claims.email or claims.user_id
    write_notification(
        req.site,
        "access_requested",
        {"requester": requester, "page": req.path},
        user_id=claims.user_id,
    )
    return {"status": "ok"}
