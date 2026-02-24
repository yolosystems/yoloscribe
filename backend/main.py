"""AgentScribe backend — FastAPI service running behind a public ALB on EKS."""

import os
import re
from typing import Any

import boto3
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from agents import ChatAgent
from agents.base import DEFAULT_MODEL, agents_prefix, skills_prefix

app = FastAPI(title="AgentScribe API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Configuration ─────────────────────────────────────────────────────────────

S3_BUCKET = os.environ.get("S3_BUCKET", "")
SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
MODEL = os.environ.get("AGENTSCRIBE_MODEL", DEFAULT_MODEL)

_aws_profile = os.environ.get("AWS_PROFILE")
_boto_session = boto3.Session(profile_name=_aws_profile) if _aws_profile else boto3.Session()
s3 = _boto_session.client("s3")
sqs = _boto_session.client("sqs") if SQS_QUEUE_URL else None

chat_agent = ChatAgent(
    s3=s3,
    bucket=S3_BUCKET,
    model_id=MODEL,
    sqs_client=sqs,
    sqs_queue_url=SQS_QUEUE_URL,
)

# ── Path safety ───────────────────────────────────────────────────────────────
# Allowed writable paths:
#   content.md
#   {page}/content.md               (child page root content)
#   .agents/{name}/agent.md         (root-page agent definition)
#   {page}/.agents/{name}/agent.md  (child-page agent definition)

AGENT_NAME_SEG = r"[a-z0-9][a-z0-9_-]*"
PAGE_SEG = r"[a-z0-9][a-z0-9_/-]*"

SAFE_PATH = re.compile(
    r"^("
    r"content\.md"
    rf"|{PAGE_SEG}/content\.md"
    rf"|\.agents/{AGENT_NAME_SEG}/agent\.md"
    rf"|{PAGE_SEG}/\.agents/{AGENT_NAME_SEG}/agent\.md"
    r")$"
)


def _is_safe_path(path: str) -> bool:
    return bool(SAFE_PATH.match(path))


def _get_content(site: str, path: str = "content.md") -> str:
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=f"{site}/{path}")
        return obj["Body"].read().decode("utf-8")
    except s3.exceptions.NoSuchKey:
        return ""


def _put_content(site: str, path: str, content: str) -> None:
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{site}/{path}",
        Body=content.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )


# ── Request / response models ─────────────────────────────────────────────────


class HistoryMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    current_content: str
    history: list[HistoryMessage] = []
    site: str = "default"
    file_path: str = "content.md"


class ChatResponse(BaseModel):
    reply: str
    updated_content: str | None = None


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/content")
async def get_content(site: str = "default", path: str = "content.md") -> Response:
    if not _is_safe_path(path):
        raise HTTPException(status_code=400, detail="Invalid path")
    content = _get_content(site, path)
    return Response(content=content, media_type="text/plain; charset=utf-8")


@app.put("/content")
async def put_content(
    request: Request, site: str = "default", path: str = "content.md"
) -> dict[str, str]:
    if not _is_safe_path(path):
        raise HTTPException(status_code=400, detail="Invalid path")
    body = await request.body()
    _put_content(site, path, body.decode("utf-8"))
    return {"status": "saved"}


@app.get("/agents")
async def list_agents(site: str = "default", page_path: str = "") -> dict:
    prefix = agents_prefix(site, page_path)
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix + "/", Delimiter="/")
    names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]
    return {"agents": names}


@app.get("/skills")
async def list_skills() -> dict:
    prefix = skills_prefix()
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix + "/", Delimiter="/")
    names = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]
    return {"skills": names}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> Any:
    if not _is_safe_path(req.file_path):
        raise HTTPException(status_code=400, detail="Invalid file_path")

    history = [
        {"role": m.role, "content": m.content}
        for m in req.history
        if m.role in ("user", "assistant")
    ]

    try:
        reply, updated_content = chat_agent.run(
            message=req.message,
            current_content=req.current_content,
            history=history,
            site=req.site,
            file_path=req.file_path,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ChatResponse(reply=reply, updated_content=updated_content)
