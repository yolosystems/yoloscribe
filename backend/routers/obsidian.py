"""Obsidian plugin sync API (/obsidian/*)

Provides five endpoints purpose-built for the YoloScribe Obsidian plugin:
  GET  /obsidian/bootstrap          — bulk page fetch for initial vault open
  GET  /obsidian/changes?since=     — delta sync (pages changed since timestamp)
  PUT  /obsidian/pages/<path>       — write a page with etag conflict detection
  GET  /obsidian/events             — SSE stream of real-time page change events
  GET  /obsidian/status             — lightweight health/metadata for status bar

Auth: API token (as_-prefixed) or JWT, resolved via get_user_context.
"""

import asyncio
import datetime
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import sse_broadcaster
from auth import get_user_context
from config import S3_BUCKET, s3
from rate_limit import limiter
from path_safety import PAGE_PATH_RE
from queue_helpers import enqueue_index_job

router = APIRouter(prefix="/obsidian", tags=["obsidian"])

_log = logging.getLogger(__name__)


# ── Path helpers ───────────────────────────────────────────────────────────────

def _s3_key(site: str, page_path: str) -> str:
    return f"{site}/{page_path}/content.md" if page_path else f"{site}/content.md"


def _page_path_from_key(site: str, key: str) -> str:
    if key == f"{site}/content.md":
        return ""
    prefix = f"{site}/"
    suffix = "/content.md"
    inner = key[len(prefix):]
    if inner.endswith(suffix):
        return inner[: -len(suffix)]
    return ""


def _list_content_objects(site: str, subtree: str = "") -> list[dict]:
    """List all content.md S3 objects under a site (or subtree prefix)."""
    prefix = f"{site}/{subtree}/" if subtree else f"{site}/"
    paginator = s3.get_paginator("list_objects_v2")
    objects = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            is_root = key == f"{site}/content.md"
            is_child = key.endswith("/content.md") and not is_root
            if not is_root and not is_child:
                continue
            # Skip content under hidden directories (.agents/, .archive/, etc.)
            inner = key[len(f"{site}/"):]
            parts = inner.split("/")
            if any(p.startswith(".") for p in parts[:-1]):
                continue
            objects.append(obj)
    return objects


def _fetch_page(site: str, obj: dict) -> dict | None:
    """Fetch content + etag for one S3 object; returns None on error."""
    key = obj["Key"]
    page_path = _page_path_from_key(site, key)
    try:
        resp = s3.get_object(Bucket=S3_BUCKET, Key=key)
        content = resp["Body"].read().decode("utf-8")
        etag = resp["ETag"]
        last_modified = obj["LastModified"]
        if last_modified.tzinfo is None:
            last_modified = last_modified.replace(tzinfo=datetime.timezone.utc)
        title = page_path.split("/")[-1].replace("-", " ").title() if page_path else "Home"
        return {
            "path": page_path,
            "title": title,
            "content": content,
            "etag": etag,
            "updated_at": last_modified.isoformat(),
        }
    except Exception:
        _log.warning("Failed to fetch content for %s", key, exc_info=True)
        return None


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/bootstrap", summary="Bulk page fetch for initial vault open")
@limiter.limit("10/minute")
async def bootstrap(
    request: Request,
    subtree: str = "",
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> JSONResponse:
    user_id, site = ctx
    if not site:
        raise HTTPException(status_code=403, detail="No site associated with this token")
    if subtree and not PAGE_PATH_RE.match(subtree):
        raise HTTPException(status_code=400, detail="Invalid subtree path")

    objects = _list_content_objects(site, subtree)
    pages = [p for obj in objects if (p := _fetch_page(site, obj)) is not None]

    return JSONResponse({
        "site": site,
        "pages": pages,
        "synced_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


@router.get("/changes", summary="Delta sync — pages changed since a timestamp")
@limiter.limit("60/minute")
async def changes(
    request: Request,
    since: str,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> JSONResponse:
    user_id, site = ctx
    if not site:
        raise HTTPException(status_code=403, detail="No site associated with this token")

    try:
        since_dt = datetime.datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid 'since' timestamp — use ISO 8601")

    objects = _list_content_objects(site)
    changed = []
    for obj in objects:
        last_modified = obj["LastModified"]
        if last_modified.tzinfo is None:
            last_modified = last_modified.replace(tzinfo=datetime.timezone.utc)
        if last_modified > since_dt:
            page = _fetch_page(site, obj)
            if page:
                changed.append(page)

    return JSONResponse({
        "changed": changed,
        "deleted": [],  # deletion tracking via SSE only in v1; soft-deletes go to .archive/
        "synced_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })


@router.put("/pages/{page_path:path}", summary="Write a page with etag conflict detection")
@limiter.limit("60/minute")
async def put_page(
    page_path: str,
    request: Request,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> JSONResponse:
    user_id, site = ctx
    if not site:
        raise HTTPException(status_code=403, detail="No site associated with this token")
    if page_path and not PAGE_PATH_RE.match(page_path):
        raise HTTPException(status_code=400, detail="Invalid page path")

    if_match = request.headers.get("If-Match")
    if not if_match:
        raise HTTPException(status_code=412, detail="If-Match header is required")

    body = await request.body()
    content = body.decode("utf-8")
    key = _s3_key(site, page_path)

    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
            IfMatch=if_match,
            Metadata={"updated-by": "obsidian"},
        )
    except s3.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] in ("PreconditionFailed", "412"):
            try:
                obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
                current_content = obj["Body"].read().decode("utf-8")
                current_etag = obj["ETag"]
            except Exception:
                current_content, current_etag = "", ""
            return JSONResponse(
                status_code=409,
                content={"detail": "Conflict", "content": current_content, "etag": current_etag},
            )
        raise

    head = s3.head_object(Bucket=S3_BUCKET, Key=key)
    new_etag = head["ETag"]

    sse_broadcaster.broadcast(site, "page_changed", {"path": page_path, "etag": new_etag, "updated_by": "obsidian"})
    enqueue_index_job(key, user_id)

    return JSONResponse({"etag": new_etag})


@router.get("/events", summary="SSE stream of real-time page change events")
@limiter.limit("10/minute")
async def events(
    request: Request,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> StreamingResponse:
    user_id, site = ctx
    if not site:
        raise HTTPException(status_code=403, detail="No site associated with this token")

    q = sse_broadcaster.register(site)

    async def _stream() -> AsyncGenerator[str, None]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield payload
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            sse_broadcaster.unregister(site, q)

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.get("/status", summary="Lightweight health and metadata for the plugin status bar")
@limiter.limit("60/minute")
async def status(
    request: Request,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> JSONResponse:
    user_id, site = ctx
    if not site:
        raise HTTPException(status_code=403, detail="No site associated with this token")

    objects = _list_content_objects(site)

    return JSONResponse({
        "site": site,
        "page_count": len(objects),
        "connected": True,
        "last_synced_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
