"""S3 helper functions, path safety, and default content for YoloScribe."""

import logging
import re

import json

from config import S3_BUCKET, S3_VECTORS_BUCKET, S3_VECTORS_INDEX_NAME, SQS_INDEXING_QUEUE_URL, s3, s3vectors, sqs_indexing

# ── Path safety ────────────────────────────────────────────────────────────────

_AGENT_NAME_SEG = r"[a-z0-9][a-z0-9_-]*"
_PAGE_SEG = r"[a-z0-9][a-z0-9_/-]*"

SAFE_PATH = re.compile(
    r"^("
    r"content\.md"
    r"|config\.json"
    r"|settings\.json"
    rf"|{_PAGE_SEG}/content\.md"
    rf"|{_PAGE_SEG}/settings\.json"
    rf"|\.agents/{_AGENT_NAME_SEG}/agent\.md"
    rf"|{_PAGE_SEG}/\.agents/{_AGENT_NAME_SEG}/agent\.md"
    rf"|\.skills/{_AGENT_NAME_SEG}/SKILL\.md"
    r"|\.user/search\.md"
    r"|\.user/notifications\.md"
    r")$"
)

SITE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
PAGE_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$")
VALID_THEMES = {"light", "dark", "yolo"}


def is_safe_path(path: str) -> bool:
    return bool(SAFE_PATH.match(path))


# ── S3 read/write ──────────────────────────────────────────────────────────────

def get_content(site: str, path: str = "content.md") -> str:
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=f"{site}/{path}")
        return obj["Body"].read().decode("utf-8")
    except s3.exceptions.NoSuchKey:
        return ""


def get_content_with_etag(site: str, path: str = "content.md") -> tuple[str, str]:
    """Return (content, etag). Raises if the object does not exist."""
    obj = s3.get_object(Bucket=S3_BUCKET, Key=f"{site}/{path}")
    return obj["Body"].read().decode("utf-8"), obj["ETag"]


def put_content(site: str, path: str, content: str) -> None:
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{site}/{path}",
        Body=content.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )


def put_content_conditional(site: str, path: str, content: str, etag: str) -> bool:
    """PUT with If-Match. Returns True on success, False on 412 conflict."""
    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=f"{site}/{path}",
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
            IfMatch=etag,
        )
        return True
    except s3.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] in ("PreconditionFailed", "412"):
            return False
        raise


def delete_s3_prefix(site_name: str) -> None:
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{site_name}/"):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if objects:
            s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": objects, "Quiet": True})


def delete_site_vectors(site_name: str) -> None:
    """Delete all S3 Vectors entries for a site (best-effort, never raises)."""
    if s3vectors is None:
        return
    paginator = s3.get_paginator("list_objects_v2")
    vector_ids: list[str] = []
    try:
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{site_name}/"):
            for obj in page.get("Contents", []):
                if "/.chunks/" in obj["Key"]:
                    vector_ids.append(obj["Key"].split("/")[-1])
    except Exception as exc:
        logging.warning("Failed to list chunks for vector deletion (%s): %s", site_name, exc)
        return
    if not vector_ids:
        return
    try:
        for i in range(0, len(vector_ids), 100):
            s3vectors.delete_vectors(
                vectorBucketName=S3_VECTORS_BUCKET,
                indexName=S3_VECTORS_INDEX_NAME,
                keys=vector_ids[i : i + 100],
            )
        logging.info("Deleted %d vectors for site %s", len(vector_ids), site_name)
    except Exception as exc:
        logging.warning("Failed to delete vectors for site %s: %s", site_name, exc)


def enqueue_index_job(content_key: str, user_id: str) -> None:
    """Send an indexing job to the SQS indexing queue (best-effort; never raises)."""
    if sqs_indexing is None or not SQS_INDEXING_QUEUE_URL:
        return
    try:
        sqs_indexing.send_message(
            QueueUrl=SQS_INDEXING_QUEUE_URL,
            MessageBody=json.dumps({"bucket": S3_BUCKET, "content_key": content_key, "user_id": user_id}),
        )
    except Exception:
        logging.warning("Failed to enqueue indexing job for %s", content_key, exc_info=True)


# ── Default content ────────────────────────────────────────────────────────────

DEFAULT_WELCOME_MD = """\
# Welcome to your YoloScribe site!

This is the home page of your personal wiki. Edit this content using the editor,
or ask the AI assistant in the Chat panel to help you write and organise your notes.

## Getting Started

- Click **Edit** to enter edit mode
- Use the **Chat** panel to ask the AI to help you write content
- Navigate to sub-pages by clicking links
"""


def default_child_page_md(title: str) -> str:
    return (
        f"# {title}\n\n"
        f"This is a new wiki page. Edit this content using the editor,\n"
        f"or ask the AI assistant in the Chat panel to help you write and organise your notes.\n\n"
        f"## Getting Started\n\n"
        f"- Click **Edit** to enter edit mode\n"
        f"- Use the **Chat** panel to ask the AI to help you write content\n"
        f"- Navigate to sub-pages by clicking links\n"
    )
