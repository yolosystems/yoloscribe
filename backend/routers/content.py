import json
import logging

import bleach
from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials

from auth import JWTClaims, decode_jwt, get_jwt_claims, get_site_for_user, get_user_context, require_site_owner, _bearer
from config import MAX_CONTENT_BYTES
from rate_limit import limiter
from s3_helpers import get_content, put_content, is_safe_path, enqueue_index_job
from settings_cache import get_page_settings, page_path_from_file_path

_audit_log = logging.getLogger("agentscribe.audit")

# HTML tags and attributes permitted in shared-write content.
# Anything not in these lists is stripped by bleach.
_ALLOWED_TAGS = list(bleach.sanitizer.ALLOWED_TAGS) + [
    "p", "br", "h1", "h2", "h3", "h4", "h5", "h6",
    "pre", "code", "blockquote", "hr",
    "table", "thead", "tbody", "tr", "th", "td",
    "img", "div", "span",
]
_ALLOWED_ATTRS = {
    **bleach.sanitizer.ALLOWED_ATTRIBUTES,
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
}

router = APIRouter()


@router.get(
    "/content",
    tags=["content"],
    summary="Get page content",
    description=(
        "Return the raw content of a page file from S3. Visibility rules apply: "
        "public pages are readable without auth; private/shared pages require a JWT. "
        "The `X-Page-Access` response header indicates the caller's access level "
        "(`full-control` | `write` | `view`)."
    ),
)
async def get_content_route(
    site: str = "default",
    path: str = "content.md",
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> Response:
    if not is_safe_path(path):
        raise HTTPException(status_code=400, detail="Invalid path")

    page_path = page_path_from_file_path(path)
    is_content_path = path == "content.md" or path.endswith("/content.md")

    # config.json is always public
    if path == "config.json":
        content = get_content(site, path)
        return Response(content=content, media_type="application/json")

    # Non-content paths are always owner-only
    if not is_content_path:
        claims = decode_jwt(credentials)
        user_site = get_site_for_user(claims.user_id)
        require_site_owner(site, user_site)
        content = get_content(site, path)
        resp = Response(content=content, media_type="text/plain; charset=utf-8")
        resp.headers["X-Page-Access"] = "full-control"
        return resp

    # Content pages: check visibility settings
    settings = get_page_settings(site, page_path)
    visibility = settings.get("visibility", "private")

    if visibility == "public":
        access = "view"
        if credentials is not None:
            try:
                claims = decode_jwt(credentials)
                user_site = get_site_for_user(claims.user_id)
                if user_site == site:
                    access = "full-control"
            except HTTPException:
                pass
        content = get_content(site, path)
        resp = Response(content=content, media_type="text/plain; charset=utf-8")
        resp.headers["X-Page-Access"] = access
        return resp

    # private or shared — authentication required
    if credentials is None:
        raise HTTPException(status_code=403, detail="Authentication required")

    claims = decode_jwt(credentials)
    user_site = get_site_for_user(claims.user_id)

    if user_site == site:
        content = get_content(site, path)
        resp = Response(content=content, media_type="text/plain; charset=utf-8")
        resp.headers["X-Page-Access"] = "full-control"
        return resp

    if visibility == "shared":
        user_email = claims.email
        shared_with = settings.get("shared_with", [])
        match = next((u for u in shared_with if u.get("email") == user_email), None)
        if match:
            content = get_content(site, path)
            resp = Response(content=content, media_type="text/plain; charset=utf-8")
            resp.headers["X-Page-Access"] = match.get("access", "view")
            return resp

    raise HTTPException(status_code=403, detail="Access denied")


@router.put(
    "/content",
    tags=["content"],
    summary="Update page content",
    description=(
        "Write raw content to a page file in S3. Requires a JWT. "
        "Site owners can write any allowed path; shared-write users may only update `content.md`."
    ),
)
@limiter.limit("60/minute")
async def put_content_route(
    request: Request,
    site: str = "default",
    path: str = "content.md",
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> dict[str, str]:
    if not is_safe_path(path):
        raise HTTPException(status_code=400, detail="Invalid path")

    claims = decode_jwt(credentials)
    user_site = get_site_for_user(claims.user_id)

    is_content_path = path == "content.md" or path.endswith("/content.md")

    is_shared_write = False
    if user_site == site:
        pass
    elif is_content_path:
        page_path = page_path_from_file_path(path)
        settings = get_page_settings(site, page_path)
        if settings.get("visibility") != "shared":
            raise HTTPException(status_code=403, detail="Access denied")
        shared_with = settings.get("shared_with", [])
        match = next((u for u in shared_with if u.get("email") == claims.email), None)
        if not match or match.get("access") != "write":
            raise HTTPException(status_code=403, detail="Access denied")
        is_shared_write = True
    else:
        raise HTTPException(status_code=403, detail="Access denied: not your site")

    body = await request.body()
    size_limit = MAX_SHARED_WRITE_BYTES if is_shared_write else MAX_CONTENT_BYTES
    if len(body) > size_limit:
        raise HTTPException(
            status_code=413,
            detail=f"Content exceeds maximum allowed size of {size_limit // 1024} KB",
        )

    text = body.decode("utf-8")

    if is_shared_write:
        # Strip dangerous HTML from shared-write content (YOL-63).
        text = bleach.clean(text, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True)
        # Emit a structured audit log entry for every shared-write save (YOL-67).
        _audit_log.info(
            json.dumps({
                "event": "shared_write",
                "site": site,
                "path": path,
                "user_email": claims.email,
                "user_id": claims.user_id,
                "bytes": len(body),
            })
        )

    put_content(site, path, text)
    if is_content_path:
        enqueue_index_job(f"{site}/{path}", claims.user_id)
    return {"status": "saved"}
