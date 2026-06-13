"""Path-safety constants and validators for YoloScribe."""

import re

_AGENT_NAME_SEG = r"[a-z0-9][a-z0-9_-]*"
_PAGE_SEG = r"[a-z0-9][a-z0-9_/-]*"
_ASSET_FILE = r"[a-zA-Z0-9][a-zA-Z0-9._-]*"
_RUN_LOG_FILE = r"\d{4}-\d{2}-\d{2}-[0-9a-f]{8}\.md"

SAFE_PATH = re.compile(
    r"^("
    r"content\.md"
    r"|config\.json"
    r"|settings\.json"
    rf"|{_PAGE_SEG}/content\.md"
    rf"|{_PAGE_SEG}/settings\.json"
    rf"|\.agents/{_AGENT_NAME_SEG}/agent\.md"
    rf"|\.agents/{_AGENT_NAME_SEG}/run_log\.md"
    rf"|\.agents/{_AGENT_NAME_SEG}/runs/{_RUN_LOG_FILE}"
    rf"|{_PAGE_SEG}/\.agents/{_AGENT_NAME_SEG}/agent\.md"
    rf"|{_PAGE_SEG}/\.agents/{_AGENT_NAME_SEG}/run_log\.md"
    rf"|{_PAGE_SEG}/\.agents/{_AGENT_NAME_SEG}/runs/{_RUN_LOG_FILE}"
    rf"|\.skills/{_AGENT_NAME_SEG}/SKILL\.md"
    r"|\.user/search\.md"
    r"|\.user/notifications\.md"
    r"|\.user/ingest/content\.md"
    rf"|\.user/ingest/{_AGENT_NAME_SEG}/content\.md"
    rf"|\.user/ingest/{_AGENT_NAME_SEG}/settings\.json"
    rf"|\.user/ingest/\.agents/{_AGENT_NAME_SEG}/agent\.md"
    rf"|\.user/ingest/\.agents/{_AGENT_NAME_SEG}/run_log\.md"
    rf"|\.user/ingest/\.agents/{_AGENT_NAME_SEG}/runs/{_RUN_LOG_FILE}"
    r")$"
)

# Matches a run log path (relative to site root, without site prefix).
# Groups: (1) page_path or None, (2) agent_name
RUN_LOG_PATH_RE = re.compile(
    r"^(?:(.+)/)?\.agents/([a-z0-9][a-z0-9_-]*)/runs/\d{4}-\d{2}-\d{2}-[0-9a-f]{8}\.md$"
)

# Asset paths: site-level or page-level, under assets/ (images) or media/ (video/audio).
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
    """Derive the page path from an asset path."""
    for sep in ("/assets/", "/media/"):
        idx = asset_path.find(sep)
        if idx != -1:
            return asset_path[:idx]
    return ""


def asset_mime_type(path: str) -> str:
    """Return the canonical MIME type for an asset path."""
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ASSET_ALLOWED_EXTENSIONS.get(ext, "application/octet-stream")


def asset_media_category(mime_type: str) -> str:
    """Return 'image', 'video', or 'audio' for a MIME type."""
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    return "audio"
