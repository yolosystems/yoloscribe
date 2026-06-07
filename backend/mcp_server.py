"""YoloScribe Remote MCP Server.

Provides wiki CRUD, search, and agent management tools over HTTP for use by
Claude Code and other MCP-compatible AI agents.

Mounted at /mcp/v1 in the FastAPI app via create_mcp_app().

Authentication: Bearer token (JWT or as_ API token). Every request must carry
  Authorization: Bearer <token>
The token is validated by the injected AuthProvider; the user's site is
resolved via the injected UserSiteRepository and stored in request.state.
"""

from __future__ import annotations

import asyncio
import datetime
import difflib
import json
import logging
import re
from typing import Any

from fastapi import HTTPException
from fastmcp import Context, FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from agent_md import (
    AGENT_NAME_RE,
    AgentDefinition,
    AgentDefinitionError,
    build_agent_md,
    parse_agent_md,
)
from k8s_agent import delete_agent_cronjob, enqueue_schedule_bootstrap
from queue_helpers import enqueue_on_write_agents
from agent_md import _parse_frontmatter
from auth_providers.base import AuthProvider, UserSiteRepository

log = logging.getLogger(__name__)

_PAGE_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$")
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# ── User context ───────────────────────────────────────────────────────────────


class _MCPUser:
    __slots__ = ("user_id", "email", "site")

    def __init__(self, user_id: str, email: str | None, site: str) -> None:
        self.user_id = user_id
        self.email = email
        self.site = site


def _user(ctx: Context) -> _MCPUser:
    return ctx.request_context.request.state.mcp_user


# ── Auth middleware ────────────────────────────────────────────────────────────


class _MCPAuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        auth_provider: AuthProvider | None,
        user_site_repo: UserSiteRepository | None,
        base_url: str = "",
        local_mode: bool = False,
        local_site_name: str = "local",
        local_user_id: str = "local-user-00000000",
        local_api_key: str = "local",
    ) -> None:
        super().__init__(app)
        self._auth_provider = auth_provider
        self._user_site_repo = user_site_repo
        self._base_url = base_url
        self._local_mode = local_mode
        self._local_site_name = local_site_name
        self._local_user_id = local_user_id
        self._local_api_key = local_api_key

    def _www_authenticate(self) -> str:
        if self._local_mode:
            return 'Bearer realm="YoloScribe (local)"'
        if self._base_url:
            metadata_url = f"{self._base_url}/.well-known/oauth-authorization-server"
            return f'Bearer realm="YoloScribe", resource_metadata="{metadata_url}"'
        return 'Bearer realm="YoloScribe"'

    async def dispatch(self, request: Request, call_next):
        # CORS preflights pass through; CORS headers are added by the parent app.
        if request.method == "OPTIONS":
            return await call_next(request)

        # In LOCAL_MODE, validate against the static API key and resolve the
        # site from LOCAL_SITE_NAME / LOCAL_USER_ID — no JWT validation needed.
        if self._local_mode:
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer ") or auth[7:] != self._local_api_key:
                return JSONResponse(
                    {"error": f"Invalid API key. Use: Authorization: Bearer {self._local_api_key}"},
                    status_code=401,
                    headers={"WWW-Authenticate": self._www_authenticate()},
                )
            request.state.mcp_user = _MCPUser(
                user_id=self._local_user_id,
                email=None,
                site=self._local_site_name,
            )
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
            claims = self._auth_provider.decode_jwt(token)
            user_id: str = claims.user_id
            email: str | None = claims.email
        except HTTPException as exc:
            return JSONResponse(
                {"error": exc.detail},
                status_code=exc.status_code,
                headers={"WWW-Authenticate": self._www_authenticate()},
            )

        site = await asyncio.to_thread(self._user_site_repo.get_site_for_user, user_id)
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


def _validate_skill_name(skill_name: str) -> None:
    if not _SKILL_NAME_RE.match(skill_name):
        raise ValueError(
            f"Invalid skill name '{skill_name}'. "
            "Use lowercase letters, digits, hyphens, and underscores."
        )


def _skill_key(site: str, skill_name: str) -> str:
    return f"{site}/.skills/{skill_name}/SKILL.md"


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
    if "/.user/" in f"/{content_key}":
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
    auth_provider: AuthProvider | None,
    user_site_repo: UserSiteRepository | None,
    sqs_indexing_client,
    sqs_indexing_queue_url: str,
    base_url: str = "",
    local_mode: bool = False,
    local_site_name: str = "local",
    local_user_id: str = "local-user-00000000",
    local_api_key: str = "local",
):
    """Create and return the FastMCP ASGI app, ready to mount at /mcp/v1."""
    mcp = FastMCP(
        "YoloScribe",
        instructions=(
            "YoloScribe is an AI-powered wiki. You can read, create, update, and delete "
            "wiki pages, run semantic or keyword searches, manage agent sessions, and "
            "list, create, and update skills. "
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
        enqueue_on_write_agents(user.site, key, user.user_id)
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

        # Include proposed diff if a pending proposal exists (YOL-244).
        proposed_key = (
            f"{user.site}/{page_path}/.proposed.content.md"
            if page_path
            else f"{user.site}/.proposed.content.md"
        )
        try:
            proposed_resp = s3_client.get_object(Bucket=bucket, Key=proposed_key)
            proposed_content = proposed_resp["Body"].read().decode("utf-8")
            diff_lines = list(difflib.unified_diff(
                content.splitlines(keepends=True),
                proposed_content.splitlines(keepends=True),
                fromfile=f"{page_path or '(root)'}/content.md (current)",
                tofile=f"{page_path or '(root)'}/.proposed.content.md",
            ))
            result["has_proposal"] = True
            result["proposed_diff"] = "".join(diff_lines) or "(no changes)"
        except Exception:
            result["has_proposal"] = False

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
        enqueue_on_write_agents(user.site, key, user.user_id)
        return {
            "page_path": page_path,
            "updated_at": _now_iso(),
            "message": message,
        }

    @mcp.tool()
    async def wiki_archive(
        page_path: str,
        ctx: Context = None,
    ) -> dict:
        """Archive a page and all its descendants.

        Copies content to .user/archive/, removes all search indexes (FTS5 + S3
        Vectors chunks), and deletes the originals. Cannot be used on the root page.

        Args:
            page_path: Page to archive (and all its descendants).
        """
        from archive_helpers import archive_page as _archive

        _validate_page_path(page_path)
        user = _user(ctx)
        result = _archive(
            s3=s3_client,
            bucket=bucket,
            site=user.site,
            page_path=page_path,
            s3vectors_client=s3vectors_client,
            vectors_bucket=vectors_bucket,
            vectors_index=vectors_index,
        )
        return {"page_path": page_path, **result}

    @mcp.tool()
    async def empty_archive(
        ctx: Context = None,
    ) -> dict:
        """Permanently delete all pages in the archive (.user/archive/).

        This is irreversible. Use with caution.
        """
        from archive_helpers import empty_archive as _empty

        user = _user(ctx)
        return _empty(s3=s3_client, bucket=bucket, site=user.site)

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

    @mcp.tool()
    async def wiki_versions(
        page_path: str,
        limit: int = 20,
        ctx: Context = None,
    ) -> dict:
        """List available versions of a wiki page.

        Args:
            page_path: Path of the page. Empty string for root page.
            limit: Maximum number of versions to return (default 20, max 50).
        """
        if page_path:
            _validate_page_path(page_path)
        user = _user(ctx)
        key = _content_key(user.site, page_path)
        limit = max(1, min(limit, 50))

        try:
            resp = s3_client.list_object_versions(Bucket=bucket, Prefix=key)
        except Exception as exc:
            log.warning("list_object_versions failed for %s: %s", key, exc)
            return {"page_path": page_path, "versions": []}

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

        return {"page_path": page_path, "versions": versions}

    @mcp.tool()
    async def wiki_diff(
        page_path: str,
        version_id: str,
        other_version_id: str = "",
        ctx: Context = None,
    ) -> dict:
        """Get a unified diff between two versions of a wiki page.

        Returns a markdown-renderable unified diff. Suitable for display directly
        in Claude Code or Claude Desktop tool output.

        Args:
            page_path: Path of the page. Empty string for root page.
            version_id: The version to diff from (the older / reference version).
            other_version_id: The version to diff to. If omitted, diffs against the current content.
        """
        if page_path:
            _validate_page_path(page_path)
        user = _user(ctx)
        key = _content_key(user.site, page_path)

        try:
            resp_a = s3_client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
            content_a = resp_a["Body"].read().decode("utf-8")
            from_label = f"{page_path or '(root)'} @ {version_id[:8]}"
        except Exception:
            raise ValueError(f"Version not found: {version_id}")

        if other_version_id:
            try:
                resp_b = s3_client.get_object(Bucket=bucket, Key=key, VersionId=other_version_id)
                content_b = resp_b["Body"].read().decode("utf-8")
                to_label = f"{page_path or '(root)'} @ {other_version_id[:8]}"
            except Exception:
                raise ValueError(f"Version not found: {other_version_id}")
        else:
            try:
                resp_b = s3_client.get_object(Bucket=bucket, Key=key)
                content_b = resp_b["Body"].read().decode("utf-8")
                to_label = f"{page_path or '(root)'} (current)"
            except Exception:
                raise ValueError(f"Page not found: '{page_path or '(root)'}'")

        diff_lines = list(difflib.unified_diff(
            content_a.splitlines(keepends=True),
            content_b.splitlines(keepends=True),
            fromfile=from_label,
            tofile=to_label,
        ))
        diff = "".join(diff_lines)

        return {
            "page_path": page_path,
            "version_id": version_id,
            "other_version_id": other_version_id or "current",
            "diff": diff or "(no changes)",
            "changed_lines": len([l for l in diff_lines if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]),
        }

    # ── Search ────────────────────────────────────────────────────────────────

    @mcp.tool()
    async def search(
        query: str,
        tags: list[str] | None = None,
        limit: int = 20,
        expand: bool = False,
        ctx: Context = None,
    ) -> dict:
        """Search wiki pages using hybrid keyword + semantic search with RRF fusion.

        Combines SQLite FTS5 (BM25 keyword) and S3 Vectors (semantic similarity)
        results via Reciprocal Rank Fusion into a single ranked list.

        Args:
            query: Search query (natural language or keywords).
            tags: Optional list of frontmatter tags to filter by (e.g. ["deployment", "ops"]).
            limit: Maximum results to return (default 20).
            expand: When True, uses Claude Haiku to generate 2-3 query variants
                    before searching, improving recall on paraphrased queries.
        """
        from hybrid_search import hybrid_search as _hybrid

        user = _user(ctx)
        limit = min(max(1, limit), 100)

        results = _hybrid(
            s3=s3_client,
            bucket=bucket,
            site=user.site,
            query=query,
            s3vectors_client=s3vectors_client,
            vectors_bucket=vectors_bucket,
            vectors_index=vectors_index,
            tags=tags,
            limit=limit,
            expand=expand,
        )
        return {"results": results, "total_hits": len(results)}

    # ── Agent management ──────────────────────────────────────────────────────
    # Agent definitions are stored as agent.md files in S3:
    #   {site}/{page_path}/.agents/{agent_name}/agent.md   (page-scoped)
    #   {site}/.agents/{agent_name}/agent.md               (root page)

    def _agent_key(site: str, page_path: str, agent_name: str) -> str:
        if page_path:
            return f"{site}/{page_path}/.agents/{agent_name}/agent.md"
        return f"{site}/.agents/{agent_name}/agent.md"

    def _agents_prefix(site: str, page_path: str) -> str:
        if page_path:
            return f"{site}/{page_path}/.agents/"
        return f"{site}/.agents/"

    def _defn_to_dict(defn: AgentDefinition, page_path: str) -> dict:
        return {
            "name": defn.name,
            "page_path": page_path,
            "trigger": defn.trigger,
            "type": defn.type,
            "description": defn.description,
            "skills": defn.skills,
            "schedule": defn.schedule,
            "timezone": defn.timezone,
            "model": defn.model,
            "confirm_before_write": defn.confirm_before_write,
            "events": defn.events,
        }

    @mcp.tool()
    async def agent_create(
        agent_name: str,
        description: str,
        skills: list[str],
        page_path: str = "",
        trigger: str = "manual",
        schedule: str = "",
        timezone: str = "",
        model: str = "",
        confirm_before_write: bool = False,
        agent_type: str = "",
        events: list[str] | None = None,
        overwrite: bool = False,
        ctx: Context = None,
    ) -> dict:
        """Create an agent.md definition on a wiki page.

        Prefer the type-specific tools (agent_create_page, agent_create_ingest,
        agent_create_notification) when the agent type is known — they enforce
        correct placement and required fields automatically.

        Args:
            agent_name: Agent name (lowercase letters, digits, hyphens, underscores).
            description: Agent purpose / system prompt instructions.
            skills: List of skill names the agent should use.
            page_path: Page to attach the agent to; empty string for the root page.
                       Overridden automatically for ingest (→ .user/ingest) and
                       notification (→ root) types.
            trigger: When the agent runs — "manual", "schedule", "on_write", or "on_notify".
            schedule: Cron expression — required when trigger is "schedule".
            timezone: Timezone for scheduled agents (e.g. "America/New_York").
            model: Model registry key (e.g. "sonnet", "opus"). Omit to use server default.
            confirm_before_write: When true the agent writes proposed changes to
                .proposed.content.md instead of content.md directly. The owner
                must accept or reject the proposal via the UI or API.
            agent_type: Agent class — "page", "ingest", or "notification". Leave
                        empty to use heuristic dispatch in the runner.
            events: Event types to watch — required when trigger is "on_notify".
                    E.g. ["page_shared", "access_requested"].
            overwrite: Set True to replace an existing agent with the same name.
        """
        user = _user(ctx)
        if not AGENT_NAME_RE.match(agent_name):
            raise ValueError(
                f"Invalid agent name '{agent_name}'. "
                "Use lowercase letters, digits, hyphens, underscores."
            )

        # Enforce canonical paths per type.
        if agent_type == "ingest":
            page_path = ".user/ingest"
        elif agent_type == "notification" or trigger == "on_notify":
            page_path = ""
        elif page_path:
            _validate_page_path(page_path)

        defn = AgentDefinition(
            name=agent_name,
            description=description,
            skills=skills or [],
            trigger=trigger,
            schedule=schedule,
            timezone=timezone,
            model=model,
            confirm_before_write=confirm_before_write,
            type=agent_type,
            events=list(events) if events else [],
        )
        try:
            build_agent_md(defn)  # validates before writing
        except AgentDefinitionError as exc:
            raise ValueError(str(exc)) from exc

        key = _agent_key(user.site, page_path, agent_name)
        if not overwrite:
            existing = s3_client.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
            if existing.get("KeyCount", 0) > 0:
                raise ValueError(
                    f"Agent '{agent_name}' already exists on page '{page_path or '(root)'}'. "
                    "Pass overwrite=True to replace it."
                )

        content = build_agent_md(defn)
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        if defn.trigger == "schedule":
            enqueue_schedule_bootstrap(key, user.user_id)
        return {"agent_name": agent_name, "page_path": page_path, "created_at": _now_iso()}

    @mcp.tool()
    async def agent_create_page(
        agent_name: str,
        description: str,
        skills: list[str],
        page_path: str = "",
        trigger: str = "manual",
        schedule: str = "",
        timezone: str = "",
        model: str = "",
        confirm_before_write: bool = False,
        overwrite: bool = False,
        ctx: Context = None,
    ) -> dict:
        """Create a page agent — reads and writes content on a specific wiki page.

        Page agents are the most common type. They watch or update a single wiki
        page and are placed under that page's .agents/ directory.

        Args:
            agent_name: Agent name (lowercase letters, digits, hyphens, underscores).
            description: Agent purpose / system prompt instructions.
            skills: List of skill names the agent should use.
            page_path: Wiki page to attach the agent to; empty string for the root page.
            trigger: "manual", "schedule", or "on_write".
            schedule: Cron expression — required when trigger is "schedule".
            timezone: Timezone for scheduled agents (e.g. "America/New_York").
            model: Model registry key (e.g. "sonnet", "opus"). Omit to use server default.
            confirm_before_write: When true the agent stages changes for owner review
                instead of writing directly to content.md.
            overwrite: Set True to replace an existing agent with the same name.
        """
        user = _user(ctx)
        if not AGENT_NAME_RE.match(agent_name):
            raise ValueError(
                f"Invalid agent name '{agent_name}'. "
                "Use lowercase letters, digits, hyphens, underscores."
            )
        if page_path:
            _validate_page_path(page_path)
        if trigger == "on_notify":
            raise ValueError(
                "Page agents cannot use trigger 'on_notify'. "
                "Use agent_create_notification for notification agents."
            )

        defn = AgentDefinition(
            name=agent_name,
            description=description,
            skills=skills or [],
            trigger=trigger,
            schedule=schedule,
            timezone=timezone,
            model=model,
            confirm_before_write=confirm_before_write,
            type="page",
        )
        try:
            build_agent_md(defn)
        except AgentDefinitionError as exc:
            raise ValueError(str(exc)) from exc

        key = _agent_key(user.site, page_path, agent_name)
        if not overwrite:
            existing = s3_client.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
            if existing.get("KeyCount", 0) > 0:
                raise ValueError(
                    f"Agent '{agent_name}' already exists on page '{page_path or '(root)'}'. "
                    "Pass overwrite=True to replace it."
                )

        content = build_agent_md(defn)
        s3_client.put_object(Bucket=bucket, Key=key, Body=content.encode("utf-8"),
                             ContentType="text/markdown; charset=utf-8")
        if defn.trigger == "schedule":
            enqueue_schedule_bootstrap(key, user.user_id)
        return {"agent_name": agent_name, "page_path": page_path, "type": "page",
                "created_at": _now_iso()}

    @mcp.tool()
    async def agent_create_ingest(
        agent_name: str,
        description: str,
        skills: list[str],
        trigger: str = "on_write",
        schedule: str = "",
        timezone: str = "",
        model: str = "",
        overwrite: bool = False,
        ctx: Context = None,
    ) -> dict:
        """Create an ingest agent — processes content staged in .user/ingest/ into wiki pages.

        Ingest agents always live at .user/ingest (enforced automatically). They
        consume uploaded or external content and write it into the wiki.

        Args:
            agent_name: Agent name (lowercase letters, digits, hyphens, underscores).
            description: Agent purpose / system prompt instructions.
            skills: List of skill names the agent should use.
            trigger: "on_write" (default, runs when content is staged) or "schedule"
                     for periodic batch processing, or "manual".
            schedule: Cron expression — required when trigger is "schedule".
            timezone: Timezone for scheduled agents (e.g. "America/New_York").
            model: Model registry key (e.g. "sonnet", "opus"). Omit to use server default.
            overwrite: Set True to replace an existing agent with the same name.
        """
        user = _user(ctx)
        if not AGENT_NAME_RE.match(agent_name):
            raise ValueError(
                f"Invalid agent name '{agent_name}'. "
                "Use lowercase letters, digits, hyphens, underscores."
            )
        if trigger == "on_notify":
            raise ValueError(
                "Ingest agents cannot use trigger 'on_notify'. "
                "Use agent_create_notification for notification agents."
            )

        page_path = ".user/ingest"
        defn = AgentDefinition(
            name=agent_name,
            description=description,
            skills=skills or [],
            trigger=trigger,
            schedule=schedule,
            timezone=timezone,
            model=model,
            type="ingest",
        )
        try:
            build_agent_md(defn)
        except AgentDefinitionError as exc:
            raise ValueError(str(exc)) from exc

        key = _agent_key(user.site, page_path, agent_name)
        if not overwrite:
            existing = s3_client.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
            if existing.get("KeyCount", 0) > 0:
                raise ValueError(
                    f"Agent '{agent_name}' already exists at .user/ingest. "
                    "Pass overwrite=True to replace it."
                )

        content = build_agent_md(defn)
        s3_client.put_object(Bucket=bucket, Key=key, Body=content.encode("utf-8"),
                             ContentType="text/markdown; charset=utf-8")
        if defn.trigger == "schedule":
            enqueue_schedule_bootstrap(key, user.user_id)
        return {"agent_name": agent_name, "page_path": page_path, "type": "ingest",
                "created_at": _now_iso()}

    @mcp.tool()
    async def agent_create_notification(
        agent_name: str,
        description: str,
        skills: list[str],
        events: list[str],
        model: str = "",
        overwrite: bool = False,
        ctx: Context = None,
    ) -> dict:
        """Create a notification agent — reacts to site events in the notifications log.

        Notification agents always live at the site root and use trigger=on_notify
        (both enforced automatically). They fire when specific event types are appended
        to the site's .user/notifications.md file.

        Available event types:
          access_requested      — a user requested access to a private page
          page_shared           — a page was shared with a user
          page_unshared         — a user was removed from a shared page
          page_access_changed   — a shared user's access level changed
          page_visibility_changed — a page's visibility setting changed
          confirm_page_change   — an agent proposed a change awaiting owner review

        Args:
            agent_name: Agent name (lowercase letters, digits, hyphens, underscores).
            description: Agent purpose / system prompt instructions.
            skills: List of skill names the agent should use.
            events: Event types this agent should handle (at least one required).
            model: Model registry key (e.g. "sonnet", "opus"). Omit to use server default.
            overwrite: Set True to replace an existing agent with the same name.
        """
        user = _user(ctx)
        if not AGENT_NAME_RE.match(agent_name):
            raise ValueError(
                f"Invalid agent name '{agent_name}'. "
                "Use lowercase letters, digits, hyphens, underscores."
            )
        if not events:
            raise ValueError(
                "Notification agents require at least one event type in 'events'. "
                "E.g. [\"page_shared\", \"access_requested\"]."
            )

        page_path = ""
        defn = AgentDefinition(
            name=agent_name,
            description=description,
            skills=skills or [],
            trigger="on_notify",
            model=model,
            type="notification",
            events=list(events),
        )
        try:
            build_agent_md(defn)
        except AgentDefinitionError as exc:
            raise ValueError(str(exc)) from exc

        key = _agent_key(user.site, page_path, agent_name)
        if not overwrite:
            existing = s3_client.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
            if existing.get("KeyCount", 0) > 0:
                raise ValueError(
                    f"Agent '{agent_name}' already exists at the site root. "
                    "Pass overwrite=True to replace it."
                )

        content = build_agent_md(defn)
        s3_client.put_object(Bucket=bucket, Key=key, Body=content.encode("utf-8"),
                             ContentType="text/markdown; charset=utf-8")
        return {"agent_name": agent_name, "page_path": page_path, "type": "notification",
                "events": list(events), "created_at": _now_iso()}

    @mcp.tool()
    async def agent_read(
        agent_name: str,
        page_path: str = "",
        ctx: Context = None,
    ) -> dict:
        """Read an agent definition from a wiki page.

        Args:
            agent_name: Name of the agent to read.
            page_path: Page the agent is attached to; empty string for the root page.
        """
        user = _user(ctx)
        key = _agent_key(user.site, page_path, agent_name)
        try:
            resp = s3_client.get_object(Bucket=bucket, Key=key)
            text = resp["Body"].read().decode("utf-8")
        except Exception:
            raise ValueError(
                f"Agent '{agent_name}' not found on page '{page_path or '(root)'}'."
            )
        try:
            defn = parse_agent_md(text)
        except AgentDefinitionError as exc:
            raise ValueError(f"agent.md is invalid: {exc}") from exc
        return _defn_to_dict(defn, page_path)

    @mcp.tool()
    async def agent_update(
        agent_name: str,
        page_path: str = "",
        description: str | None = None,
        skills: list[str] | None = None,
        trigger: str | None = None,
        schedule: str | None = None,
        timezone: str | None = None,
        model: str | None = None,
        confirm_before_write: bool | None = None,
        ctx: Context = None,
    ) -> dict:
        """Update fields of an existing agent definition.

        Only the fields you supply are changed; omitted fields keep their current values.

        Args:
            agent_name: Name of the agent to update.
            page_path: Page the agent is attached to; empty string for the root page.
            description: New agent description / system prompt.
            skills: Replacement skills list.
            trigger: New trigger type — "manual", "schedule", "on_write", or "on_notify".
            schedule: New cron expression (required if changing trigger to "schedule").
            timezone: New timezone.
            model: New model key. Pass empty string to clear.
            confirm_before_write: Set or clear the propose-mode flag.
        """
        user = _user(ctx)
        key = _agent_key(user.site, page_path, agent_name)
        try:
            resp = s3_client.get_object(Bucket=bucket, Key=key)
            text = resp["Body"].read().decode("utf-8")
        except Exception:
            raise ValueError(
                f"Agent '{agent_name}' not found on page '{page_path or '(root)'}'."
            )
        try:
            defn = parse_agent_md(text)
        except AgentDefinitionError as exc:
            raise ValueError(f"Existing agent.md is invalid: {exc}") from exc

        updated = AgentDefinition(
            name=defn.name,
            description=description if description is not None else defn.description,
            skills=skills if skills is not None else defn.skills,
            trigger=trigger if trigger is not None else defn.trigger,
            schedule=schedule if schedule is not None else defn.schedule,
            timezone=timezone if timezone is not None else defn.timezone,
            model=model if model is not None else defn.model,
            confirm_before_write=confirm_before_write if confirm_before_write is not None else defn.confirm_before_write,
            type=defn.type,
            events=defn.events,
        )
        try:
            content = build_agent_md(updated)
        except AgentDefinitionError as exc:
            raise ValueError(str(exc)) from exc

        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        if updated.trigger == "schedule":
            enqueue_schedule_bootstrap(key, user.user_id)
        elif defn.trigger == "schedule" and updated.trigger != "schedule":
            delete_agent_cronjob(user.site, agent_name, user.user_id)
        return {"agent_name": agent_name, "page_path": page_path, "updated_at": _now_iso()}

    @mcp.tool()
    async def agent_delete(
        agent_name: str,
        page_path: str = "",
        ctx: Context = None,
    ) -> dict:
        """Delete an agent definition from a wiki page.

        Args:
            agent_name: Name of the agent to delete.
            page_path: Page the agent is attached to; empty string for the root page.
        """
        user = _user(ctx)

        # Read trigger before deleting so we know whether to clean up a CronJob.
        was_scheduled = False
        agent_key = _agent_key(user.site, page_path, agent_name)
        try:
            resp = s3_client.get_object(Bucket=bucket, Key=agent_key)
            text = resp["Body"].read().decode("utf-8")
            was_scheduled = parse_agent_md(text).trigger == "schedule"
        except Exception:
            pass  # best-effort; proceed with delete regardless

        prefix = f"{_agents_prefix(user.site, page_path)}{agent_name}/"
        paginator = s3_client.get_paginator("list_objects_v2")
        keys_deleted = 0
        for s3_page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            objects = [{"Key": obj["Key"]} for obj in s3_page.get("Contents", [])]
            if objects:
                s3_client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
                keys_deleted += len(objects)
        if keys_deleted == 0:
            raise ValueError(
                f"Agent '{agent_name}' not found on page '{page_path or '(root)'}'."
            )

        if was_scheduled:
            delete_agent_cronjob(user.site, agent_name, user.user_id)

        return {"agent_name": agent_name, "page_path": page_path, "deleted": True}

    @mcp.tool()
    async def agent_list(
        page_path: str = "",
        site_wide: bool = False,
        ctx: Context = None,
    ) -> dict:
        """List agent definitions on a page, or across the whole site.

        Args:
            page_path: Page to list agents for; empty string for the root page.
                       Ignored when site_wide is True.
            site_wide: When True, returns all agents across all pages in the site.
        """
        user = _user(ctx)
        agents: list[dict] = []

        if site_wide:
            # Walk all objects under {site}/ looking for /.agents/*/agent.md
            paginator = s3_client.get_paginator("list_objects_v2")
            site_prefix = f"{user.site}/"
            for s3_page in paginator.paginate(Bucket=bucket, Prefix=site_prefix):
                for obj in s3_page.get("Contents", []):
                    key: str = obj["Key"]
                    if not key.endswith("/agent.md"):
                        continue
                    # Key shape: {site}/{page_path}/.agents/{name}/agent.md
                    #        or: {site}/.agents/{name}/agent.md
                    rel = key[len(site_prefix):]
                    if "/.agents/" not in rel and not rel.startswith(".agents/"):
                        continue
                    try:
                        text = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
                        defn = parse_agent_md(text)
                        # Derive page_path from key
                        if rel.startswith(".agents/"):
                            pg = ""
                        else:
                            pg = rel.split("/.agents/")[0]
                        agents.append(_defn_to_dict(defn, pg))
                    except Exception:
                        pass
        else:
            prefix = _agents_prefix(user.site, page_path)
            paginator = s3_client.get_paginator("list_objects_v2")
            for s3_page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
                for cp in s3_page.get("CommonPrefixes", []):
                    agent_key = f"{cp['Prefix']}agent.md"
                    try:
                        text = s3_client.get_object(Bucket=bucket, Key=agent_key)["Body"].read().decode()
                        defn = parse_agent_md(text)
                        agents.append(_defn_to_dict(defn, page_path))
                    except Exception:
                        pass

        return {"agents": agents}

    # ── Skills ────────────────────────────────────────────────────────────────
    # Skills are stored at {site}/.skills/{skill_name}/SKILL.md.
    # The file format is a YAML frontmatter block followed by markdown instructions.

    @mcp.tool()
    async def skill_list(ctx: Context = None) -> dict:
        """List all skills defined for the user's site.

        Returns each skill's name, description, and referenced tools.
        """
        user = _user(ctx)
        prefix = f"{user.site}/.skills/"
        skills: list[dict] = []
        paginator = s3_client.get_paginator("list_objects_v2")
        for s3_page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            for cp in s3_page.get("CommonPrefixes", []):
                skill_name = cp["Prefix"][len(prefix):].rstrip("/")
                key = f"{cp['Prefix']}SKILL.md"
                try:
                    resp = s3_client.get_object(Bucket=bucket, Key=key)
                    text = resp["Body"].read().decode("utf-8")
                    fm, _ = _parse_frontmatter(text)
                    tools_list = fm.get("tools", [])
                    if isinstance(tools_list, str):
                        tools_list = [tools_list]
                    skills.append(
                        {
                            "name": skill_name,
                            "description": fm.get("description", ""),
                            "tools": tools_list,
                            "updated_at": resp["LastModified"].isoformat(),
                        }
                    )
                except Exception:
                    skills.append({"name": skill_name, "description": "", "tools": []})
        return {"skills": skills}

    @mcp.tool()
    async def skill_create(
        skill_name: str,
        content: str,
        ctx: Context = None,
    ) -> dict:
        """Create a new skill (SKILL.md) for the user's site.

        Args:
            skill_name: Skill name (lowercase letters, digits, hyphens, underscores).
            content: Full SKILL.md content including YAML frontmatter.
        """
        _validate_skill_name(skill_name)
        user = _user(ctx)
        key = _skill_key(user.site, skill_name)
        existing = s3_client.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
        if existing.get("KeyCount", 0) > 0:
            raise ValueError(
                f"Skill '{skill_name}' already exists. Use skill_update to modify it."
            )
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        return {"skill_name": skill_name, "created_at": _now_iso()}

    @mcp.tool()
    async def skill_update(
        skill_name: str,
        content: str,
        ctx: Context = None,
    ) -> dict:
        """Update an existing skill's SKILL.md content.

        Args:
            skill_name: Name of the skill to update.
            content: New full SKILL.md content including YAML frontmatter.
        """
        _validate_skill_name(skill_name)
        user = _user(ctx)
        key = _skill_key(user.site, skill_name)
        try:
            s3_client.head_object(Bucket=bucket, Key=key)
        except Exception:
            raise ValueError(
                f"Skill '{skill_name}' does not exist. Use skill_create to create it."
            )
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        return {"skill_name": skill_name, "updated_at": _now_iso()}

    @mcp.tool()
    async def skill_delete(
        skill_name: str,
        ctx: Context = None,
    ) -> dict:
        """Permanently delete a skill and all its files (SKILL.md, mcp.json, etc.).

        Args:
            skill_name: Name of the skill to delete.
        """
        _validate_skill_name(skill_name)
        user = _user(ctx)
        prefix = _skill_key(user.site, skill_name).rsplit("/", 1)[0] + "/"
        paginator = s3_client.get_paginator("list_objects_v2")
        to_delete = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                to_delete.append({"Key": obj["Key"]})
        if not to_delete:
            raise ValueError(f"Skill '{skill_name}' not found")
        s3_client.delete_objects(Bucket=bucket, Delete={"Objects": to_delete, "Quiet": True})
        return {"skill_name": skill_name, "deleted": True}

    # ── Introspection ─────────────────────────────────────────────────────────

    @mcp.tool()
    async def list_skill_tools(ctx: Context = None) -> dict:
        """List all tools available to agents running on this site.

        Reads the tool declarations from each installed skill and returns a
        consolidated list of every tool agents can call, grouped by the skill
        that provides it. Useful for discovering whether a particular tool
        (e.g. a Linear or GitHub tool) is already available before asking the
        user to create a new skill.
        """
        user = _user(ctx)
        prefix = f"{user.site}/.skills/"
        tools: list[dict] = []
        paginator = s3_client.get_paginator("list_objects_v2")
        for s3_page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            for cp in s3_page.get("CommonPrefixes", []):
                skill_name = cp["Prefix"][len(prefix):].rstrip("/")
                key = f"{cp['Prefix']}SKILL.md"
                try:
                    resp = s3_client.get_object(Bucket=bucket, Key=key)
                    text = resp["Body"].read().decode("utf-8")
                    fm, _ = _parse_frontmatter(text)
                    tools_list = fm.get("tools", [])
                    if isinstance(tools_list, str):
                        tools_list = [tools_list]
                    for tool_name in tools_list:
                        tools.append({"name": tool_name, "skill": skill_name})
                except Exception:
                    pass
        return {"tools": tools}

    # ── Return ASGI app ───────────────────────────────────────────────────────

    return mcp.http_app(
        path="/",
        middleware=[
            Middleware(
                _MCPAuthMiddleware,
                auth_provider=auth_provider,
                user_site_repo=user_site_repo,
                base_url=base_url,
                local_mode=local_mode,
                local_site_name=local_site_name,
                local_user_id=local_user_id,
                local_api_key=local_api_key,
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
