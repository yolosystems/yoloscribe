import json
import re

from fastapi import APIRouter, Depends, HTTPException

from agents.base import AGENT_NAME_RE, agents_prefix, skills_prefix
from auth import get_user_context, require_site_owner
from config import S3_BUCKET, s3
from models import CreateAgentRequest, CreatePageRequest
from s3_helpers import PAGE_PATH_RE, default_child_page_md, enqueue_index_job

router = APIRouter()


def _extract_agent_meta(text: str) -> dict:
    """Extract trigger and scope from agent.md frontmatter."""
    fm_match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    trigger = "manual"
    scope: list[str] = []
    is_pointer = False
    if fm_match:
        fm = fm_match.group(1)
        tm = re.search(r"^trigger:\s*(\S+)", fm, re.MULTILINE)
        if tm:
            trigger = tm.group(1)
        if re.search(r"^scope:", fm, re.MULTILINE):
            scope = re.findall(r"^\s+-\s+(.+)$", fm, re.MULTILINE)
        if re.search(r"^ref:\s*\S+", fm, re.MULTILINE):
            is_pointer = True
    return {"trigger": trigger, "scope": scope, "is_pointer": is_pointer}


@router.get("/agents", tags=["agents"], summary="List agents for a page")
async def list_agents(site: str = "default", page_path: str = "") -> dict:
    prefix = agents_prefix(site, page_path)
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix + "/", Delimiter="/")
    names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]

    agents = []
    for name in names:
        key = f"{prefix}/{name}/agent.md"
        meta: dict = {"trigger": "manual", "scope": [], "is_pointer": False}
        try:
            obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
            text = obj["Body"].read().decode("utf-8")
            meta = _extract_agent_meta(text)
        except Exception:
            pass
        agents.append({"name": name, **meta})

    return {"agents": agents}


@router.post("/agents", tags=["agents"], summary="Create a new agent")
async def create_agent(
    req: CreateAgentRequest,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    user_id, user_site = ctx
    require_site_owner(req.site, user_site)

    if not AGENT_NAME_RE.match(req.agent_name):
        raise HTTPException(
            status_code=400,
            detail="Invalid agent name: use lowercase letters, digits, hyphens, and underscores",
        )

    prefix = agents_prefix(req.site, req.page_path)
    key = f"{prefix}/{req.agent_name}/agent.md"

    check = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=key, MaxKeys=1)
    if check.get("KeyCount", 0) > 0:
        raise HTTPException(status_code=409, detail="Agent already exists")

    skeleton = (
        f"---\ntrigger: manual\n---\n\n"
        f"# Agent: {req.agent_name}\n\n"
        f"## Description\n\nDescribe what this agent does.\n\n"
        f"## Skills\n\n- (none)\n"
    )
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=skeleton.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    return {"agent_name": req.agent_name}


@router.delete("/agents", tags=["agents"], summary="Delete an agent")
async def delete_agent(
    site: str,
    agent_name: str,
    page_path: str = "",
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    user_id, user_site = ctx
    require_site_owner(site, user_site)

    if not AGENT_NAME_RE.match(agent_name):
        raise HTTPException(status_code=400, detail="Invalid agent name")

    prefix = agents_prefix(site, page_path)
    key = f"{prefix}/{agent_name}/agent.md"

    check = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=key, MaxKeys=1)
    if check.get("KeyCount", 0) == 0:
        raise HTTPException(status_code=404, detail="Agent not found")

    s3.delete_object(Bucket=S3_BUCKET, Key=key)
    return {"deleted": agent_name}


@router.get("/pages", tags=["pages"], summary="List child pages")
async def list_pages(site: str = "default", page_path: str = "") -> dict:
    prefix = f"{site}/{page_path}/" if page_path else f"{site}/"
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix, Delimiter="/")
    pages = []
    for cp in resp.get("CommonPrefixes", []):
        name = cp["Prefix"][len(prefix):].rstrip("/")
        if name.startswith("."):
            continue
        content_key = f"{cp['Prefix']}content.md"
        check = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=content_key, MaxKeys=1)
        if check.get("KeyCount", 0) > 0:
            pages.append(name)
    return {"pages": pages}


@router.post("/pages", tags=["pages"], summary="Create a new page")
async def create_page(
    req: CreatePageRequest,
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    user_id, user_site = ctx
    require_site_owner(req.site, user_site)

    if not PAGE_PATH_RE.match(req.page_path):
        raise HTTPException(
            status_code=400,
            detail="Invalid page path: use lowercase letters, digits, hyphens, and underscores separated by slashes",
        )

    content_key = f"{req.site}/{req.page_path}/content.md"
    check = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=content_key, MaxKeys=1)
    if check.get("KeyCount", 0) > 0:
        raise HTTPException(status_code=409, detail="Page already exists")

    title = req.page_path.split("/")[-1].replace("-", " ").title()
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=content_key,
        Body=default_child_page_md(title).encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{req.site}/{req.page_path}/.agents/.keep",
        Body=b"",
        ContentType="text/plain",
    )
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{req.site}/{req.page_path}/settings.json",
        Body=json.dumps({"visibility": "private", "shared_with": []}).encode("utf-8"),
        ContentType="application/json",
    )
    enqueue_index_job(content_key, user_id)
    return {"page_path": req.page_path, "content_key": content_key}


@router.get("/skills", tags=["skills"], summary="List skills for a site")
async def list_skills(
    site: str = "default",
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    """Return all skill names defined in the site's .skills directory."""
    user_id, user_site = ctx
    require_site_owner(site, user_site)
    prefix = skills_prefix(site)
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix + "/", Delimiter="/")
    names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]
    return {"skills": names}
