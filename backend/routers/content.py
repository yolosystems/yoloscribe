import json
import logging

import bleach
from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials

import sse_broadcaster
from yoloscribe_io import AgentDefinitionError, parse_agent_md
from auth import JWTClaims, decode_jwt, get_jwt_claims, get_site_for_user, get_user_context, require_site_owner, _bearer
from config import MAX_CONTENT_BYTES, MAX_SHARED_WRITE_BYTES
from k8s_agent import enqueue_schedule_bootstrap
from rate_limit import limiter
from path_safety import is_safe_path
from queue_helpers import enqueue_index_job, enqueue_on_write_agents
from s3_storage import storage
from settings_cache import get_page_settings, page_path_from_file_path
from yoloscribe_io import WikiPageMarkdownFile

_audit_log = logging.getLogger("yoloscribe.audit")

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


def _get_proposed(site: str, page_path: str) -> str | None:
    proposed = f"{page_path}/.proposed.content.md" if page_path else ".proposed.content.md"
    return storage.read(f"{site}/{proposed}")


def _delete_proposed(site: str, page_path: str) -> None:
    proposed = f"{page_path}/.proposed.content.md" if page_path else ".proposed.content.md"
    storage.delete(f"{site}/{proposed}")


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
        content = storage.read(f"{site}/{path}") or ""
        return Response(content=content, media_type="application/json")

    # Non-content paths are always owner-only
    if not is_content_path:
        _, user_site = get_user_context(credentials)
        require_site_owner(site, user_site)
        content = storage.read(f"{site}/{path}") or ""
        resp = Response(content=content, media_type="text/plain; charset=utf-8")
        resp.headers["X-Page-Access"] = "full-control"
        return resp

    # Content pages: check visibility settings
    settings = get_page_settings(site, page_path)
    visibility = settings.get("visibility", "private")
    wiki = WikiPageMarkdownFile(site=site, page_path=page_path, storage=storage)

    if visibility == "public":
        access = "view"
        if credentials is not None:
            try:
                _, user_site = get_user_context(credentials)
                if user_site == site:
                    access = "full-control"
            except HTTPException:
                pass
        content, etag = wiki.read_with_etag()
        resp = Response(content=content, media_type="text/plain; charset=utf-8")
        resp.headers["X-Page-Access"] = access
        if etag:
            resp.headers["ETag"] = etag
        return resp

    # private or shared — authentication required
    if credentials is None:
        raise HTTPException(status_code=403, detail="Authentication required")

    _, user_site = get_user_context(credentials)

    if user_site == site:
        content, etag = wiki.read_with_etag()
        resp = Response(content=content, media_type="text/plain; charset=utf-8")
        resp.headers["X-Page-Access"] = "full-control"
        if etag:
            resp.headers["ETag"] = etag
        return resp

    if visibility == "shared":
        # API tokens are always owner-scoped so they never reach here;
        # shared access requires an email from a JWT.
        try:
            user_email = decode_jwt(credentials).email
        except HTTPException:
            raise HTTPException(status_code=403, detail="Access denied")
        shared_with = settings.get("shared_with", [])
        match = next((u for u in shared_with if u.get("email") == user_email), None)
        if match:
            content, etag = wiki.read_with_etag()
            resp = Response(content=content, media_type="text/plain; charset=utf-8")
            resp.headers["X-Page-Access"] = match.get("access", "view")
            if etag:
                resp.headers["ETag"] = etag
            return resp

    raise HTTPException(status_code=403, detail="Access denied")


def _maybe_bootstrap_schedule(site: str, path: str, text: str, user_id: str) -> None:
    try:
        defn = parse_agent_md(text)
    except AgentDefinitionError:
        return
    if defn.trigger == "schedule":
        enqueue_schedule_bootstrap(f"{site}/{path}", user_id)


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
) -> Response:
    if not is_safe_path(path):
        raise HTTPException(status_code=400, detail="Invalid path")
    if path == ".user/notifications.md":
        raise HTTPException(status_code=403, detail="notifications.md is platform-controlled and cannot be written directly")

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

    if_match = request.headers.get("If-Match")

    if is_content_path:
        page_path = page_path_from_file_path(path)
        wiki = WikiPageMarkdownFile(site=site, page_path=page_path, storage=storage)
        if if_match:
            saved = wiki.write_conditional(text, if_match, user_id=claims.user_id)
            if not saved:
                return Response(
                    content='{"detail":"Conflict: the page was modified by another writer. Reload and try again."}',
                    status_code=409,
                    media_type="application/json",
                )
        else:
            wiki.write(text, user_id=claims.user_id)
        enqueue_index_job(wiki.key, claims.user_id)
        if not is_shared_write:
            enqueue_on_write_agents(site, wiki.key, claims.user_id)
        sse_broadcaster.broadcast(site, "page_changed", {"path": page_path, "updated_by": "web"})
    else:
        if if_match:
            saved = storage.write_conditional(f"{site}/{path}", text, if_match)
            if not saved:
                return Response(
                    content='{"detail":"Conflict: the page was modified by another writer. Reload and try again."}',
                    status_code=409,
                    media_type="application/json",
                )
        else:
            storage.write(f"{site}/{path}", text)
        if ".agents/" in path and path.endswith("agent.md") and not is_shared_write:
            _maybe_bootstrap_schedule(site, path, text, claims.user_id)

    return Response(content='{"status":"saved"}', status_code=200, media_type="application/json")


@router.post(
    "/accept-proposed",
    tags=["content"],
    summary="Accept a proposed page change",
    description=(
        "Apply a pending proposed change by writing .proposed.content.md to content.md, "
        "then deleting the proposed file. Requires authentication as the site owner."
    ),
)
async def accept_proposed_route(
    site: str = "default",
    page_path: str = "",
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> Response:
    claims = decode_jwt(credentials)
    user_site = get_site_for_user(claims.user_id)
    require_site_owner(site, user_site)

    proposed = _get_proposed(site, page_path)
    if proposed is None:
        raise HTTPException(status_code=404, detail="No pending proposal for this page")

    wiki = WikiPageMarkdownFile(site=site, page_path=page_path, storage=storage)
    wiki.write(proposed, user_id=claims.user_id)
    _delete_proposed(site, page_path)

    enqueue_index_job(wiki.key, claims.user_id)
    enqueue_on_write_agents(site, wiki.key, claims.user_id)
    sse_broadcaster.broadcast(site, "page_changed", {"path": page_path, "updated_by": "agent"})

    return Response(content='{"status":"accepted"}', status_code=200, media_type="application/json")


@router.post(
    "/reject-proposed",
    tags=["content"],
    summary="Reject a proposed page change",
    description=(
        "Discard a pending proposed change by deleting .proposed.content.md. "
        "Requires authentication as the site owner."
    ),
)
async def reject_proposed_route(
    site: str = "default",
    page_path: str = "",
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> Response:
    _, user_site = get_user_context(credentials)
    require_site_owner(site, user_site)

    proposed = _get_proposed(site, page_path)
    if proposed is None:
        raise HTTPException(status_code=404, detail="No pending proposal for this page")

    _delete_proposed(site, page_path)
    return Response(content='{"status":"rejected"}', status_code=200, media_type="application/json")


@router.get(
    "/proposed",
    tags=["content"],
    summary="Get proposed page content",
    description=(
        "Return the pending proposed content for a page, written by an agent with "
        "confirm_before_write: true. Returns 404 if no proposal is pending. "
        "Requires authentication as the site owner."
    ),
)
async def get_proposed_route(
    site: str = "default",
    page_path: str = "",
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> Response:
    _, user_site = get_user_context(credentials)
    require_site_owner(site, user_site)

    proposed = _get_proposed(site, page_path)
    if proposed is None:
        raise HTTPException(status_code=404, detail="No pending proposal for this page")
    return Response(content=proposed, media_type="text/plain; charset=utf-8")
