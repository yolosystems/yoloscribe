"""S3 helper functions, path safety, and default content for YoloScribe."""

import logging
import re

import json

from config import S3_BUCKET, S3_VECTORS_BUCKET, S3_VECTORS_INDEX_NAME, SQS_INDEXING_QUEUE_URL, SQS_QUEUE_URL, s3, s3vectors, sqs, sqs_indexing

# ── Path safety ────────────────────────────────────────────────────────────────

_AGENT_NAME_SEG = r"[a-z0-9][a-z0-9_-]*"
_PAGE_SEG = r"[a-z0-9][a-z0-9_/-]*"
_ASSET_FILE = r"[a-zA-Z0-9][a-zA-Z0-9._-]*"

SAFE_PATH = re.compile(
    r"^("
    r"content\.md"
    r"|config\.json"
    r"|settings\.json"
    rf"|{_PAGE_SEG}/content\.md"
    rf"|{_PAGE_SEG}/settings\.json"
    rf"|\.agents/{_AGENT_NAME_SEG}/agent\.md"
    rf"|\.agents/{_AGENT_NAME_SEG}/run_log\.md"
    rf"|{_PAGE_SEG}/\.agents/{_AGENT_NAME_SEG}/agent\.md"
    rf"|{_PAGE_SEG}/\.agents/{_AGENT_NAME_SEG}/run_log\.md"
    rf"|\.skills/{_AGENT_NAME_SEG}/SKILL\.md"
    r"|\.user/search\.md"
    r"|\.user/notifications\.md"
    r")$"
)

# Asset paths: site-level or page-level, under assets/ (images) or media/ (video/audio).
# Filenames may contain letters, digits, dots, hyphens, and underscores only —
# no path separators, preventing traversal within the directory.
ASSET_PATH_RE = re.compile(rf"^({_PAGE_SEG}/)?(assets|media)/{_ASSET_FILE}$")

# Allowed media extensions mapped to their canonical MIME types.
ASSET_ALLOWED_EXTENSIONS: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".m4a": "audio/mp4",
}

# Maximum upload sizes enforced via pre-signed URL ContentLengthRange condition.
ASSET_MAX_BYTES: dict[str, int] = {
    "image": 20 * 1024 * 1024,    # 20 MB
    "video": 500 * 1024 * 1024,   # 500 MB
    "audio": 100 * 1024 * 1024,   # 100 MB
}

SITE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
PAGE_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$")
VALID_THEMES = {"light", "dark", "yolo"}


def is_safe_path(path: str) -> bool:
    return bool(SAFE_PATH.match(path))


def is_safe_asset_path(path: str) -> bool:
    """Return True if path is a valid asset path with an allowed extension."""
    if not ASSET_PATH_RE.match(path):
        return False
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext in ASSET_ALLOWED_EXTENSIONS


def asset_page_path(asset_path: str) -> str:
    """Derive the page path from an asset path.

    "assets/foo.png"          → ""          (root page)
    "intro/assets/foo.mp4"    → "intro"
    "a/b/assets/foo.jpg"      → "a/b"
    "intro/media/video.mp4"   → "intro"
    "a/b/media/video.mp4"     → "a/b"
    """
    for sep in ("/assets/", "/media/"):
        idx = asset_path.find(sep)
        if idx != -1:
            return asset_path[:idx]
    return ""


def asset_mime_type(path: str) -> str:
    """Return the canonical MIME type for an asset path, or 'application/octet-stream'."""
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ASSET_ALLOWED_EXTENSIONS.get(ext, "application/octet-stream")


def asset_media_category(mime_type: str) -> str:
    """Return 'image', 'video', or 'audio' for a MIME type."""
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    return "audio"


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


def _proposed_path(path: str) -> str:
    """Return the .proposed.content.md path corresponding to a content.md path."""
    return path[: -len("content.md")] + ".proposed.content.md"


def get_proposed(site: str, page_path: str) -> str | None:
    """Read .proposed.content.md for a page. Returns None if not found."""
    content_md = f"{page_path}/content.md" if page_path else "content.md"
    proposed = _proposed_path(content_md)
    obj_key = f"{site}/{proposed}"
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=obj_key)
        return obj["Body"].read().decode("utf-8")
    except s3.exceptions.NoSuchKey:
        return None


def delete_proposed(site: str, page_path: str) -> None:
    """Delete .proposed.content.md for a page (best-effort; never raises)."""
    content_md = f"{page_path}/content.md" if page_path else "content.md"
    proposed = _proposed_path(content_md)
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=f"{site}/{proposed}")
    except Exception as exc:
        logging.warning("Failed to delete proposed for %s/%s: %s", site, page_path, exc)


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


def enqueue_on_notify_agents(site: str, entry_text: str, user_id: str) -> None:
    """Enqueue agent-runner jobs for any on_notify agents on the site's root page.

    Looks in {site}/.agents/ for agents with trigger: on_notify. Passes the
    triggering notification entry as the prompt so the agent sees only the new
    event. Best-effort; never raises.
    """
    if sqs is None or not SQS_QUEUE_URL:
        return

    agents_prefix = f"{site}/.agents/"
    try:
        resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=agents_prefix)
    except Exception:
        logging.warning("Failed to list on_notify agents for site %s", site, exc_info=True)
        return

    for obj in resp.get("Contents", []):
        key = obj["Key"]
        if not key.endswith("/agent.md"):
            continue
        try:
            agent_text = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read().decode("utf-8")
        except Exception:
            logging.warning("Failed to read agent.md at %s", key, exc_info=True)
            continue

        trigger_m = re.search(r"^trigger:\s*(\S+)", agent_text, re.MULTILINE)
        if not trigger_m or trigger_m.group(1) != "on_notify":
            continue

        prompt = (
            "A new notification has been added to notifications.md:\n\n"
            f"{entry_text.strip()}\n\n"
            "Process this notification according to your instructions."
        )

        try:
            sqs.send_message(
                QueueUrl=SQS_QUEUE_URL,
                MessageBody=json.dumps({
                    "bucket": S3_BUCKET,
                    "agent_md_key": key,
                    "content_key": f"{site}/.user/notifications.md",
                    "prompt": prompt,
                    "user_id": user_id,
                }),
            )
            logging.info("Enqueued on_notify agent %s for site %s", key, site)
        except Exception:
            logging.warning("Failed to enqueue on_notify agent %s", key, exc_info=True)


def enqueue_on_write_agents(site: str, content_key: str, user_id: str) -> None:
    """Enqueue agent-runner jobs for any on_write agents subscribed to this page.

    Lists .agents/ under the written page's directory and queues a K8s Job via SQS
    for each agent.md with trigger: on_write. Best-effort; never raises.
    """
    if sqs is None or not SQS_QUEUE_URL:
        return

    # Strip trailing /content.md to get the page directory S3 prefix.
    if not content_key.endswith("/content.md"):
        return
    page_dir = content_key[: -len("/content.md")]
    agents_prefix = f"{page_dir}/.agents/"

    try:
        resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=agents_prefix)
    except Exception:
        logging.warning("Failed to list on_write agents for %s", content_key, exc_info=True)
        return

    for obj in resp.get("Contents", []):
        key = obj["Key"]
        if not key.endswith("/agent.md"):
            continue
        try:
            agent_text = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read().decode("utf-8")
        except Exception:
            logging.warning("Failed to read agent.md at %s", key, exc_info=True)
            continue

        trigger_m = re.search(r"^trigger:\s*(\S+)", agent_text, re.MULTILINE)
        if not trigger_m or trigger_m.group(1) != "on_write":
            continue

        try:
            sqs.send_message(
                QueueUrl=SQS_QUEUE_URL,
                MessageBody=json.dumps({
                    "bucket": S3_BUCKET,
                    "agent_md_key": key,
                    "content_key": content_key,
                    "prompt": "A page in your scope has been updated. Review it and apply any necessary updates to your tracked pages.",
                    "user_id": user_id,
                }),
            )
            logging.info("Enqueued on_write agent %s for %s", key, content_key)
        except Exception:
            logging.warning("Failed to enqueue on_write agent %s", key, exc_info=True)


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
