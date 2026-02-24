"""AgentScribe backend — FastAPI service running behind a public ALB on EKS."""

import os
import re
from typing import Any

import anthropic
import boto3
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from agents import ChatAgent

app = FastAPI(title="AgentScribe API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Configuration ────────────────────────────────────────────────────────────

S3_BUCKET = os.environ.get("S3_BUCKET", "")
MODEL = "claude-opus-4-6"

_aws_profile = os.environ.get("AWS_PROFILE")
_boto_session = boto3.Session(profile_name=_aws_profile) if _aws_profile else boto3.Session()
s3 = _boto_session.client("s3")

claude = anthropic.Anthropic()

chat_agent = ChatAgent(client=claude, s3=s3, bucket=S3_BUCKET, model=MODEL)

# ── Helpers ───────────────────────────────────────────────────────────────────

SAFE_PATH = re.compile(r'^(content\.md|agents/[a-z0-9][a-z0-9_-]*/agents\.md)$')


def _is_safe_path(path: str) -> bool:
    return bool(SAFE_PATH.match(path))


def _get_content(site: str) -> str:
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=f"{site}/content.md")
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


# ── Routes ───────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/content")
async def get_content(site: str = "default") -> Response:
    content = _get_content(site)
    return Response(content=content, media_type="text/plain; charset=utf-8")


@app.put("/content")
async def put_content(request: Request, site: str = "default", path: str = "content.md") -> dict[str, str]:
    if not _is_safe_path(path):
        raise HTTPException(status_code=400, detail="Invalid path")
    body = await request.body()
    _put_content(site, path, body.decode("utf-8"))
    return {"status": "saved"}


@app.get("/skills")
async def list_skills() -> dict:
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix="skills/", Delimiter="/")
    names = [p["Prefix"].split("/")[1] for p in resp.get("CommonPrefixes", [])]
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
    except anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ChatResponse(reply=reply, updated_content=updated_content)
