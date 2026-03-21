"""AgentScribe Remote MCP Server.

Provides wiki CRUD, search, and agent management tools over HTTP for use by
Claude Code and other MCP-compatible AI agents.

Mounted at /mcp/v1 in the FastAPI app via create_mcp_app().

Authentication: Bearer token (Supabase JWT). Every request must carry
  Authorization: Bearer <supabase-jwt>
The token is validated against the Supabase JWKS endpoint; the user's site is
resolved from the user_site table and stored in request.state for all tools.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import time
import uuid
from typing import Any

import httpx
import jwt as pyjwt
from fastmcp import Context, FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

_PAGE_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$")

# ── User context ───────────────────────────────────────────────────────────────


class _MCPUser:
    __slots__ = ("user_id", "email", "site")

    def __init__(self, user_id: str, email: str | None, site: str) -> None:
        self.user_id = user_id
        self.email = email
        self.site = site


def _user(ctx: Context) -> _MCPUser:
    return ctx.request_context.request.state.mcp_user


# ── Site lookup cache (5-minute TTL) ──────────────────────────────────────────

_site_cache: dict[str, tuple[str | None, float]] = {}


async def _lookup_site(user_id: str, supabase_url: str, supabase_key: str) -> str | None:
    """Resolve user_id → site_name via Supabase, with a 5-minute in-memory cache."""
    now = time.time()
    if user_id in _site_cache:
        site, ts = _site_cache[user_id]
        if now - ts < 300:
            return site

    site: str | None = None
    if supabase_url and supabase_key:
        url = f"{supabase_url}/rest/v1/user_site?user_uuid=eq.{user_id}&select=site_name&limit=1"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {supabase_key}",
                        "apikey": supabase_key,
                    },
                )
                rows = resp.json()
                if isinstance(rows, list) and rows:
                    site = rows[0].get("site_name")
        except Exception as exc:
            log.warning("Site lookup failed for user %s: %s", user_id, exc)

    _site_cache[user_id] = (site, now)
    return site


# ── Auth middleware ────────────────────────────────────────────────────────────


class _MCPAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, jwks_client, supabase_url: str, supabase_key: str, base_url: str = "") -> None:
        super().__init__(app)
        self._jwks = jwks_client
        self._supa_url = supabase_url
        self._supa_key = supabase_key
        self._base_url = base_url

    def _www_authenticate(self) -> str:
        if self._base_url:
            metadata_url = f"{self._base_url}/.well-known/oauth-authorization-server"
            return f'Bearer realm="AgentScribe", resource_metadata="{metadata_url}"'
        return 'Bearer realm="AgentScribe"'

    async def dispatch(self, request: Request, call_next):
        # CORS preflights pass through; CORS headers are added by the parent app.
        if request.method == "OPTIONS":
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "Missing or invalid Authorization header"},
                status_code=401,
                headers={"WWW-Authenticate": self._www_authenticate()},
            )

        token = auth[7:]
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            payload = pyjwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience="authenticated",
            )
            user_id: str = payload["sub"]
            email: str | None = payload.get("email")
        except pyjwt.exceptions.PyJWTError as exc:
            return JSONResponse(
                {"error": f"Invalid token: {exc}"},
                status_code=401,
                headers={"WWW-Authenticate": self._www_authenticate()},
            )

        site = await _lookup_site(user_id, self._supa_url, self._supa_key)
        if not site:
            return JSONResponse(
                {"error": "No site provisioned for this account. Please sign up first."},
                status_code=403,
            )

        request.state.mcp_user = _MCPUser(user_id=user_id, email=email, site=site)
        return await call_next(request)


# ── S3 key helpers ─────────────────────────────────────────────────────────────
#
# Defense-in-depth: every S3 key constructed in this file is prefixed with
# `user.site` obtained from the authenticated JWT — never from a user-supplied
# `site` parameter.  MCP tools accept `page_path` (a relative path within the
# user's own site) but never a raw `site` argument, so cross-site access via
# crafted inputs is structurally impossible at this layer.
#
# If S3Tools from agents/base.py is ever adopted here, instantiate it with
# `user_site=user.site` so the ownership check at that layer is also enforced.


def _content_key(site: str, page_path: str) -> str:
    return f"{site}/{page_path}/content.md" if page_path else f"{site}/content.md"


def _settings_key(site: str, page_path: str) -> str:
    return f"{site}/{page_path}/settings.json" if page_path else f"{site}/settings.json"


def _is_internal(relative_key: str) -> bool:
    """True for S3 keys that belong to internal prefixes (.agents, .skills, etc.)."""
    parts = relative_key.split("/")
    return any(p.startswith(".") for p in parts[:-1])  # exclude last segment (filename)


def _validate_page_path(page_path: str) -> None:
    if page_path and not _PAGE_PATH_RE.match(page_path):
        raise ValueError(
            f"Invalid page path '{page_path}'. "
            "Use lowercase letters, digits, hyphens, underscores, and forward slashes."
        )


def _now_iso() -> str:
    return datetime.datetime.now(tz=datetime.timezone.utc).isoformat()


def _maybe_enqueue_index(
    content_key: str,
    user_id: str,
    bucket: str,
    sqs_client,
    queue_url: str,
) -> None:
    if sqs_client is None or not queue_url:
        return
    try:
        sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(
                {"bucket": bucket, "content_key": content_key, "user_id": user_id}
            ),
        )
    except Exception as exc:
        log.warning("Failed to enqueue indexing job for %s: %s", content_key, exc)


# ── MCP app factory ────────────────────────────────────────────────────────────


def create_mcp_app(
    *,
    s3_client,
    bucket: str,
    s3vectors_client,
    vectors_bucket: str,
    vectors_index: str,
    bedrock_embedding_model: str,
    bedrock_region: str,
    jwks_client,
    supabase_url: str,
    supabase_service_role_key: str,
    sqs_indexing_client,
    sqs_indexing_queue_url: str,
    base_url: str = "",
):
    """Create and return the FastMCP ASGI app, ready to mount at /mcp/v1."""
    mcp = FastMCP(
        "AgentScribe",
        instructions=(
            "AgentScribe is an AI-powered wiki. You can read, create, update, and delete "
            "wiki pages, run semantic or keyword searches, and manage agent sessions. "
            "All operations are scoped to the authenticated user's site."
        ),
    )

    # ── Wiki CRUD ─────────────────────────────────────────────────────────────

    @mcp.tool()
    async def wiki_create(page_path: str, content: str, ctx: Context) -> dict:
        """Create a new wiki page with markdown content.

        Args:
            page_path: Relative path (e.g. "features/auth"). Empty string for root page.
            content: Full markdown content for the page.
        """
        _validate_page_path(page_path)
        user = _user(ctx)
        key = _content_key(user.site, page_path)
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        # Write default private settings.json if one doesn't exist yet.
        sk = _settings_key(user.site, page_path)
        try:
            s3_client.head_object(Bucket=bucket, Key=sk)
        except Exception:
            s3_client.put_object(
                Bucket=bucket,
                Key=sk,
                Body=json.dumps({"visibility": "private", "shared_with": []}).encode(),
                ContentType="application/json",
            )
        _maybe_enqueue_index(key, user.user_id, bucket, sqs_indexing_client, sqs_indexing_queue_url)
        return {
            "page_path": page_path,
            "url": f"/{user.site}/{page_path}" if page_path else f"/{user.site}/",
            "created_at": _now_iso(),
        }

    @mcp.tool()
    async def wiki_read(
        page_path: str,
        include_metadata: bool = False,
        ctx: Context = None,
    ) -> dict:
        """Retrieve a wiki page's content.

        Args:
            page_path: Path of the page to retrieve. Empty string for root page.
            include_metadata: Include last-modified timestamp, size, and child page list.
        """
        _validate_page_path(page_path)
        user = _user(ctx)
        key = _content_key(user.site, page_path)
        try:
            resp = s3_client.get_object(Bucket=bucket, Key=key)
        except s3_client.exceptions.NoSuchKey:
            raise ValueError(f"Page not found: '{page_path or '(root)'}'")
        content = resp["Body"].read().decode("utf-8")
        result: dict[str, Any] = {
            "page_path": page_path,
            "content": content,
            "last_updated": resp["LastModified"].isoformat(),
        }
        if include_metadata:
            result["size_bytes"] = resp["ContentLength"]
            result["children"] = _list_immediate_children(user.site, page_path, s3_client, bucket)
        return result

    @mcp.tool()
    async def wiki_update(
        page_path: str,
        content: str,
        message: str = "",
        ctx: Context = None,
    ) -> dict:
        """Update an existing wiki page's content.

        Args:
            page_path: Path to update. Empty string for root page.
            content: New full markdown content.
            message: Optional change summary for audit purposes.
        """
        _validate_page_path(page_path)
        user = _user(ctx)
        key = _content_key(user.site, page_path)
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        _maybe_enqueue_index(key, user.user_id, bucket, sqs_indexing_client, sqs_indexing_queue_url)
        return {
            "page_path": page_path,
            "updated_at": _now_iso(),
            "message": message,
        }

    @mcp.tool()
    async def wiki_delete(
        page_path: str,
        reason: str = "",
        ctx: Context = None,
    ) -> dict:
        """Soft-delete a wiki page (archived under .archive/ prefix).

        Args:
            page_path: Path to delete.
            reason: Optional deletion reason for audit trail.
        """
        _validate_page_path(page_path)
        user = _user(ctx)
        key = _content_key(user.site, page_path)
        archive_key = (
            f"{user.site}/.archive/{page_path}/content.md"
            if page_path
            else f"{user.site}/.archive/_root/content.md"
        )
        try:
            s3_client.copy_object(
                CopySource={"Bucket": bucket, "Key": key},
                Bucket=bucket,
                Key=archive_key,
            )
        except Exception:
            raise ValueError(f"Page not found: '{page_path or '(root)'}'")
        s3_client.delete_object(Bucket=bucket, Key=key)
        deleted_at = _now_iso()
        return {
            "page_path": page_path,
            "deleted_at": deleted_at,
            "archived": True,
            "reason": reason,
        }

    @mcp.tool()
    async def wiki_list(
        page_path: str = "",
        recursive: bool = True,
        limit: int = 100,
        ctx: Context = None,
    ) -> dict:
        """List wiki pages under a path.

        Args:
            page_path: Root path to list from. Empty string lists all pages.
            recursive: Include nested child pages (default True).
            limit: Maximum results (default 100, max 500).
        """
        if page_path:
            _validate_page_path(page_path)
        user = _user(ctx)
        limit = min(max(1, limit), 500)
        prefix = f"{user.site}/{page_path}/" if page_path else f"{user.site}/"

        pages: list[dict] = []
        paginator = s3_client.get_paginator("list_objects_v2")
        for s3_page in paginator.paginate(
            Bucket=bucket,
            Prefix=prefix,
            PaginationConfig={"MaxItems": limit * 5},
        ):
            for obj in s3_page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith("/content.md"):
                    continue
                relative = key[len(f"{user.site}/"):]
                if _is_internal(relative):
                    continue
                path = "" if relative == "content.md" else relative[: -len("/content.md")]

                if not recursive:
                    # Only include direct children of page_path
                    within = path[len(page_path) + 1 :] if (page_path and path.startswith(page_path + "/")) else (path if not page_path else None)
                    if within is None or "/" in within:
                        continue

                title = path.split("/")[-1].replace("-", " ").title() if path else "(root)"
                pages.append(
                    {
                        "path": path,
                        "title": title,
                        "updated_at": obj["LastModified"].isoformat(),
                        "size_bytes": obj["Size"],
                    }
                )
                if len(pages) >= limit:
                    break
            if len(pages) >= limit:
                break

        return {"pages": pages}

    # ── Search ────────────────────────────────────────────────────────────────

    @mcp.tool()
    async def search_wiki(
        query: str,
        page_path_prefix: str = "",
        limit: int = 20,
        ctx: Context = None,
    ) -> dict:
        """Keyword search across wiki pages in the user's site.

        Args:
            query: Search term or phrase (case-insensitive).
            page_path_prefix: Limit search to pages under this path prefix.
            limit: Maximum results to return (default 20).
        """
        user = _user(ctx)
        limit = min(max(1, limit), 100)
        prefix = (
            f"{user.site}/{page_path_prefix}/"
            if page_path_prefix
            else f"{user.site}/"
        )
        query_lower = query.lower()
        results: list[dict] = []

        paginator = s3_client.get_paginator("list_objects_v2")
        for s3_page in paginator.paginate(
            Bucket=bucket,
            Prefix=prefix,
            PaginationConfig={"MaxItems": 300},
        ):
            for obj in s3_page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith("/content.md"):
                    continue
                relative = key[len(f"{user.site}/"):]
                if _is_internal(relative):
                    continue

                try:
                    content_obj = s3_client.get_object(Bucket=bucket, Key=key)
                    text = content_obj["Body"].read().decode("utf-8")
                except Exception:
                    continue

                if query_lower not in text.lower():
                    continue

                idx = text.lower().find(query_lower)
                start = max(0, idx - 100)
                end = min(len(text), idx + 200)
                excerpt = text[start:end].strip()
                path = "" if relative == "content.md" else relative[: -len("/content.md")]
                results.append(
                    {
                        "page_path": path,
                        "score": 1.0,
                        "excerpt": excerpt,
                        "updated_at": obj["LastModified"].isoformat(),
                    }
                )
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                break

        return {"results": results, "total_hits": len(results)}

    @mcp.tool()
    async def search_semantic(
        query: str,
        limit: int = 20,
        min_score: float = 0.0,
        ctx: Context = None,
    ) -> dict:
        """Semantic vector search across wiki pages using embeddings.

        Args:
            query: Natural language query.
            limit: Number of results (default 20).
            min_score: Minimum similarity score threshold (0.0-1.0).
        """
        import boto3

        if not s3vectors_client or not vectors_bucket:
            raise ValueError(
                "Semantic search is not configured on this server (S3_VECTORS_BUCKET not set)."
            )

        user = _user(ctx)
        limit = min(max(1, limit), 50)

        # Embed the query
        bedrock = boto3.client("bedrock-runtime", region_name=bedrock_region)
        embed_resp = bedrock.invoke_model(
            modelId=bedrock_embedding_model,
            body=json.dumps({"inputText": query}),
        )
        embedding: list[float] = json.loads(embed_resp["body"].read())["embedding"]

        # Over-fetch so we have enough results after filtering by site
        raw = s3vectors_client.query_vectors(
            vectorBucketName=vectors_bucket,
            indexName=vectors_index,
            queryVector={"float32": embedding},
            topK=min(limit * 5, 100),
            returnMetadata=True,
        )

        results: list[dict] = []
        site_prefix = f"{user.site}/"
        for vec in raw.get("vectors", []):
            metadata = vec.get("metadata", {})
            path = metadata.get("path", "")
            score = float(vec.get("score", 0.0))

            if not path.startswith(site_prefix):
                continue
            if score < min_score:
                continue

            # Fetch the chunk text for a content preview
            preview = ""
            try:
                page_dir = path.rsplit("/", 1)[0]
                chunk_key = f"{page_dir}/.chunks/{vec['key']}"
                chunk_obj = s3_client.get_object(Bucket=bucket, Key=chunk_key)
                chunk_data = json.loads(chunk_obj["Body"].read())
                preview = chunk_data.get("text", "")[:500]
            except Exception:
                pass

            relative = path[len(site_prefix):]
            page_path_val = "" if relative == "content.md" else relative[: -len("/content.md")]
            results.append(
                {
                    "page_path": page_path_val,
                    "similarity_score": score,
                    "content_preview": preview,
                }
            )
            if len(results) >= limit:
                break

        return {"results": results}

    # ── Agent management ──────────────────────────────────────────────────────
    # Agent sessions are stored at {site}/.mcp/agents/{agent_id}/meta.json
    # and {site}/.mcp/agents/{agent_id}/context.json.
    # These are distinct from agent.md definitions used by the AgentScribe runner.

    @mcp.tool()
    async def agent_create(
        agent_name: str,
        description: str = "",
        config: dict = None,
        ctx: Context = None,
    ) -> dict:
        """Register a new agent session.

        Args:
            agent_name: Human-readable name for the agent session.
            description: Purpose and capabilities of this agent.
            config: Optional JSON configuration object.
        """
        user = _user(ctx)
        agent_id = str(uuid.uuid4())
        now = _now_iso()
        meta = {
            "agent_id": agent_id,
            "name": agent_name,
            "description": description,
            "config": config or {},
            "status": "active",
            "created_at": now,
            "last_activity": now,
        }
        key = f"{user.site}/.mcp/agents/{agent_id}/meta.json"
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(meta).encode(),
            ContentType="application/json",
        )
        return {"agent_id": agent_id, "created_at": now, "status": "active"}

    @mcp.tool()
    async def agent_get_status(agent_id: str, ctx: Context = None) -> dict:
        """Retrieve the status and metadata of an agent session.

        Args:
            agent_id: Agent identifier returned by agent_create.
        """
        user = _user(ctx)
        key = f"{user.site}/.mcp/agents/{agent_id}/meta.json"
        try:
            resp = s3_client.get_object(Bucket=bucket, Key=key)
            return json.loads(resp["Body"].read())
        except Exception:
            raise ValueError(f"Agent not found: '{agent_id}'")

    @mcp.tool()
    async def agent_update_context(
        agent_id: str,
        context: dict,
        ttl: int = 0,
        ctx: Context = None,
    ) -> dict:
        """Store state/context for an agent session.

        Args:
            agent_id: Agent identifier.
            context: Arbitrary JSON state to persist (replaces previous context).
            ttl: Time-to-live in seconds. 0 means no expiry.
        """
        user = _user(ctx)
        context_id = str(uuid.uuid4())
        now = _now_iso()
        ctx_data = {
            "context_id": context_id,
            "data": context,
            "created_at": now,
            "expires_at": (time.time() + ttl) if ttl > 0 else None,
        }
        ctx_key = f"{user.site}/.mcp/agents/{agent_id}/context.json"
        s3_client.put_object(
            Bucket=bucket,
            Key=ctx_key,
            Body=json.dumps(ctx_data).encode(),
            ContentType="application/json",
        )
        # Update last_activity in meta
        meta_key = f"{user.site}/.mcp/agents/{agent_id}/meta.json"
        try:
            meta_resp = s3_client.get_object(Bucket=bucket, Key=meta_key)
            meta = json.loads(meta_resp["Body"].read())
            meta["last_activity"] = now
            s3_client.put_object(
                Bucket=bucket,
                Key=meta_key,
                Body=json.dumps(meta).encode(),
                ContentType="application/json",
            )
        except Exception:
            pass
        return {"agent_id": agent_id, "context_id": context_id, "updated_at": now}

    @mcp.tool()
    async def agent_get_context(
        agent_id: str,
        context_id: str = "",
        ctx: Context = None,
    ) -> dict:
        """Retrieve stored context for an agent session.

        Args:
            agent_id: Agent identifier.
            context_id: Specific context ID to retrieve. If omitted, returns the latest.
        """
        user = _user(ctx)
        ctx_key = f"{user.site}/.mcp/agents/{agent_id}/context.json"
        try:
            resp = s3_client.get_object(Bucket=bucket, Key=ctx_key)
            ctx_data = json.loads(resp["Body"].read())
        except Exception:
            raise ValueError(f"No context found for agent '{agent_id}'")

        expires_at = ctx_data.get("expires_at")
        if expires_at and time.time() > expires_at:
            raise ValueError(f"Context for agent '{agent_id}' has expired")

        return {
            "context": ctx_data.get("data", {}),
            "context_id": ctx_data.get("context_id", ""),
            "retrieved_at": _now_iso(),
        }

    @mcp.tool()
    async def agent_list(limit: int = 50, ctx: Context = None) -> dict:
        """List active agent sessions for the current user.

        Args:
            limit: Maximum results (default 50).
        """
        user = _user(ctx)
        limit = min(max(1, limit), 200)
        prefix = f"{user.site}/.mcp/agents/"
        agents: list[dict] = []
        paginator = s3_client.get_paginator("list_objects_v2")
        for s3_page in paginator.paginate(
            Bucket=bucket,
            Prefix=prefix,
            Delimiter="/",
            PaginationConfig={"MaxItems": limit * 2},
        ):
            for cp in s3_page.get("CommonPrefixes", []):
                meta_key = f"{cp['Prefix']}meta.json"
                try:
                    meta_resp = s3_client.get_object(Bucket=bucket, Key=meta_key)
                    meta = json.loads(meta_resp["Body"].read())
                    agents.append(
                        {
                            "agent_id": meta["agent_id"],
                            "name": meta["name"],
                            "status": meta["status"],
                            "last_activity": meta["last_activity"],
                        }
                    )
                except Exception:
                    pass
                if len(agents) >= limit:
                    break
            if len(agents) >= limit:
                break

        return {"agents": agents}

    # ── Return ASGI app ───────────────────────────────────────────────────────

    return mcp.http_app(
        path="/",
        middleware=[
            Middleware(
                _MCPAuthMiddleware,
                jwks_client=jwks_client,
                supabase_url=supabase_url,
                supabase_key=supabase_service_role_key,
                base_url=base_url,
            )
        ],
    )


# ── Helpers ────────────────────────────────────────────────────────────────────


def _list_immediate_children(
    site: str, page_path: str, s3_client, bucket: str
) -> list[str]:
    """Return names of direct child pages (1 level deep)."""
    prefix = f"{site}/{page_path}/" if page_path else f"{site}/"
    children: list[str] = []
    try:
        resp = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")
        for cp in resp.get("CommonPrefixes", []):
            relative = cp["Prefix"][len(f"{site}/"):]
            name = relative.rstrip("/")
            if not name.startswith("."):
                children.append(name)
    except Exception:
        pass
    return children
