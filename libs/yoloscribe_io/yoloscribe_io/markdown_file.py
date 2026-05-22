from __future__ import annotations

from typing import Any

from .events import EventEmitter, EventType
from .storage import StorageBackend


class MarkdownFile(EventEmitter):
    """Base class for all yoloscribe-io file types.

    site and path are bound at construction and never accepted as method
    arguments. This means LLM tool wrappers exposing read() and write()
    cannot be coerced into operating on an arbitrary path.
    """

    def __init__(
        self,
        site: str,
        path: str,
        storage: StorageBackend,
        content: str | None = None,
    ) -> None:
        super().__init__()
        self._site = site
        self._path = path
        self._storage = storage
        self._raw_content: str | None = content

    # ── Identity ───────────────────────────────────────────────────────────────

    @property
    def site(self) -> str:
        return self._site

    @property
    def path(self) -> str:
        return self._path

    @property
    def key(self) -> str:
        """Full storage key: {site}/{path}"""
        return f"{self._site}/{self._path}"

    # ── Content ────────────────────────────────────────────────────────────────

    @property
    def raw_content(self) -> str:
        if self._raw_content is None:
            self._raw_content = self._storage.read(self.key) or ""
        return self._raw_content

    @property
    def frontmatter(self) -> dict[str, Any]:
        return _parse_frontmatter(self.raw_content)[0]

    @property
    def content(self) -> str:
        """Body text after the frontmatter block."""
        return _parse_frontmatter(self.raw_content)[1]

    # ── I/O ────────────────────────────────────────────────────────────────────

    def read(self) -> str:
        """Fetch latest content from storage and return it."""
        self._raw_content = self._storage.read(self.key) or ""
        self._emit(EventType.PAGE_READ, {"key": self.key})
        return self._raw_content

    def write(self, raw_content: str) -> None:
        """Persist raw_content to storage."""
        self._storage.write(self.key, raw_content)
        self._raw_content = raw_content
        self._emit(EventType.PAGE_WRITTEN, {"key": self.key})


# ── Frontmatter parsing ────────────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body).

    Returns ({}, text) when no valid frontmatter block is present so callers
    always get a consistent shape regardless of file format.
    """
    if not text.startswith("---"):
        return {}, text

    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    fm_text = text[3:end]
    body = text[end + 4:].lstrip("\n")

    try:
        import yaml
        fm = yaml.safe_load(fm_text)
    except Exception:
        return {}, body

    return (fm if isinstance(fm, dict) else {}), body
