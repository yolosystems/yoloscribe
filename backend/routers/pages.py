import json
import re

from fastapi import APIRouter, Depends, HTTPException

from yoloscribe_io import AgentDefinitionError, parse_agent_md
from agents.base import AGENT_NAME_RE, agents_prefix, skills_prefix
from auth import get_user_context, require_site_owner
from config import S3_BUCKET, s3
from defaults import default_child_page_md
from k8s_agent import delete_agent_cronjob
from models import CreateAgentRequest, CreatePageRequest
from path_safety import PAGE_PATH_RE
from queue_helpers import enqueue_index_job

router = APIRouter()


def _extract_agent_meta(text: str) -> dict:
    """Extract trigger, scope, and eval_log from agent.md frontmatter."""
    fm_match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    trigger = "manual"
    scope: list[str] = []
    is_pointer = False
    eval_log = False
    if fm_match:
        fm = fm_match.group(1)
        tm = re.search(r"^trigger:\s*(\S+)", fm, re.MULTILINE)
        if tm:
            trigger = tm.group(1)
        if re.search(r"^scope:", fm, re.MULTILINE):
            scope = re.findall(r"^\s+-\s+(.+)$", fm, re.MULTILINE)
        if re.search(r"^ref:\s*\S+", fm, re.MULTILINE):
            is_pointer = True
        if re.search(r"^eval_log:\s*true", fm, re.MULTILINE):
            eval_log = True
    return {"trigger": trigger, "scope": scope, "is_pointer": is_pointer, "eval_log": eval_log}


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
        f"---\ntrigger: manual\nname: {req.agent_name}\nskills:\n---\n\n"
        f"Describe what this agent does.\n"
    )
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=skeleton.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    return {"agent_name": req.agent_name}


@router.get("/agent-runs", tags=["agents"], summary="List annotation run logs for an agent")
async def list_agent_runs(
    site: str = "default",
    agent_name: str = "",
    page_path: str = "",
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    """List available eval annotation run log files for a given agent.

    Returns filenames (YYYY-MM-DD-{8hex}.md) in reverse chronological order.
    Requires site ownership — run logs are owner-only.
    """
    user_id, user_site = ctx
    require_site_owner(site, user_site)

    if not AGENT_NAME_RE.match(agent_name):
        raise HTTPException(status_code=400, detail="Invalid agent name")

    prefix = agents_prefix(site, page_path)
    runs_prefix = f"{prefix}/{agent_name}/runs/"

    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=runs_prefix)
    runs = []
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        filename = key[len(runs_prefix):]
        if filename.endswith(".md"):
            runs.append(filename)

    runs.sort(reverse=True)
    return {"runs": runs}


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
    agent_prefix = f"{prefix}/{agent_name}/"

    # Read trigger before deleting so we know whether to clean up a CronJob.
    was_scheduled = False
    agent_md_key = f"{prefix}/{agent_name}/agent.md"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=agent_md_key)
        text = obj["Body"].read().decode("utf-8")
        defn = parse_agent_md(text)
        was_scheduled = defn.trigger == "schedule"
    except Exception:
        pass  # best-effort; proceed with delete regardless

    paginator = s3.get_paginator("list_objects_v2")
    keys_to_delete: list[dict] = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=agent_prefix):
        for obj in page.get("Contents", []):
            keys_to_delete.append({"Key": obj["Key"]})

    if not keys_to_delete:
        raise HTTPException(status_code=404, detail="Agent not found")

    s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": keys_to_delete})

    if was_scheduled:
        delete_agent_cronjob(site, agent_name, user_id)

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


@router.delete("/skill", tags=["skills"], summary="Delete a skill")
async def delete_skill(
    site: str = "default",
    skill_name: str = "",
    ctx: tuple[str, str | None] = Depends(get_user_context),
) -> dict:
    """Permanently delete a skill and all its files (.SKILL.md, mcp.json, etc.).
    Requires site ownership.
    """
    user_id, user_site = ctx
    require_site_owner(site, user_site)

    if not skill_name or not re.match(r"^[a-z0-9][a-z0-9_-]*$", skill_name):
        raise HTTPException(status_code=400, detail="Invalid skill name")

    prefix = f"{skills_prefix(site)}/{skill_name}/"
    paginator = s3.get_paginator("list_objects_v2")
    to_delete = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            to_delete.append({"Key": obj["Key"]})

    if not to_delete:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")

    s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": to_delete, "Quiet": True})
    return {"skill_name": skill_name, "deleted": True}
