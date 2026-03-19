"""In-memory TTL cache for page access-control settings."""

import json
import threading
import time

from s3_helpers import get_content

_settings_cache: dict[str, tuple[dict, float]] = {}
_settings_cache_lock = threading.Lock()
_SETTINGS_CACHE_TTL = 60.0  # seconds


def page_path_from_file_path(path: str) -> str:
    """Return the page_path (S3 prefix segment) for a given file path.

    Examples:
        "content.md"              → ""
        "blog/content.md"         → "blog"
        "blog/posts/content.md"   → "blog/posts"
        "settings.json"           → ""
        "blog/settings.json"      → "blog"
        ".agents/foo/agent.md"    → ""
        "blog/.agents/foo/agent.md" → "blog"
    """
    if "/" not in path:
        return ""
    for suffix in ("/content.md", "/settings.json"):
        if path.endswith(suffix):
            return path[: -len(suffix)]
    agents_idx = path.find("/.agents/")
    if agents_idx != -1:
        return path[:agents_idx]
    return ""


def get_page_settings(site: str, page_path: str) -> dict:
    """Return parsed settings.json for a page (with in-memory TTL cache)."""
    cache_key = f"{site}/{page_path}"
    now = time.time()
    with _settings_cache_lock:
        if cache_key in _settings_cache:
            data, ts = _settings_cache[cache_key]
            if now - ts < _SETTINGS_CACHE_TTL:
                return data
    s3_path = "settings.json" if not page_path else f"{page_path}/settings.json"
    raw = get_content(site, s3_path)
    data: dict = json.loads(raw) if raw else {"visibility": "private", "shared_with": []}
    with _settings_cache_lock:
        _settings_cache[cache_key] = (data, now)
    return data


def invalidate_settings_cache(site: str, page_path: str) -> None:
    cache_key = f"{site}/{page_path}"
    with _settings_cache_lock:
        _settings_cache.pop(cache_key, None)
