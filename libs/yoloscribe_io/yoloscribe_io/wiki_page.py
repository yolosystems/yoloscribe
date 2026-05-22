from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Literal

from .events import Event, EventEmitter, EventHandler, EventType
from .markdown_file import MarkdownFile
from .storage import StorageBackend


# ── Settings data model ───────────────────────────────────────────────────────

@dataclass
class SharedUser:
    email: str
    access: Literal["view", "write"]

    def to_dict(self) -> dict:
        return {"email": self.email, "access": self.access}


@dataclass
class SettingsData:
    visibility: Literal["public", "private", "shared"] = "private"
    shared_with: list[SharedUser] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "visibility": self.visibility,
            "shared_with": [u.to_dict() for u in self.shared_with],
        }

    @classmethod
    def from_dict(cls, d: dict) -> SettingsData:
        return cls(
            visibility=d.get("visibility", "private"),
            shared_with=[
                SharedUser(email=u["email"], access=u["access"])
                for u in d.get("shared_with", [])
                if "email" in u and "access" in u
            ],
        )

    @classmethod
    def default(cls) -> SettingsData:
        return cls(visibility="private", shared_with=[])


# ── PageSettings ──────────────────────────────────────────────────────────────

class PageSettings(EventEmitter):
    """Reads and writes a page's settings.json, emitting settings.changed on save."""

    def __init__(self, site: str, page_path: str, storage: StorageBackend) -> None:
        super().__init__()
        self._site = site
        self._page_path = page_path
        self._storage = storage
        self._data: SettingsData | None = None

    @property
    def key(self) -> str:
        if self._page_path:
            return f"{self._site}/{self._page_path}/settings.json"
        return f"{self._site}/settings.json"

    def load(self) -> SettingsData:
        raw = self._storage.read(self.key)
        self._data = SettingsData.from_dict(json.loads(raw)) if raw else SettingsData.default()
        return self._data

    def save(self, data: SettingsData) -> None:
        old = self._data
        self._storage.write(self.key, json.dumps(data.to_dict()))
        self._data = data
        self._emit(EventType.SETTINGS_CHANGED, {
            "site": self._site,
            "page_path": self._page_path,
            "old": old.to_dict() if old is not None else None,
            "new": data.to_dict(),
        })

    def request_access(self, requester: str) -> None:
        """Emit access.requested — caller is responsible for persisting a notification."""
        self._emit(EventType.ACCESS_REQUESTED, {
            "site": self._site,
            "page_path": self._page_path,
            "requester": requester,
        })


# ── WikiPageMarkdownFile ──────────────────────────────────────────────────────

class WikiPageMarkdownFile(MarkdownFile):
    """A wiki page content file at {site}/{page_path}/content.md.

    Overrides write() to include page_path and user_id in the event payload so
    that OnWriteEventHandler can dispatch on_write agents without needing to
    inspect the key string.
    """

    def __init__(
        self,
        site: str,
        page_path: str,
        storage: StorageBackend,
        content: str | None = None,
    ) -> None:
        path = f"{page_path}/content.md" if page_path else "content.md"
        super().__init__(site, path, storage, content)
        self._page_path = page_path

    @property
    def page_path(self) -> str:
        return self._page_path

    def write(self, raw_content: str, user_id: str = "") -> None:
        """Persist raw_content and emit page.written with page_path and user_id."""
        self._storage.write(self.key, raw_content)
        self._raw_content = raw_content
        self._emit(EventType.PAGE_WRITTEN, {
            "key": self.key,
            "site": self._site,
            "page_path": self._page_path,
            "user_id": user_id,
        })

    def create(self, initial_content: str = "", user_id: str = "") -> None:
        """Write initial content and emit page.created."""
        self._storage.write(self.key, initial_content)
        self._raw_content = initial_content
        self._emit(EventType.PAGE_CREATED, {
            "key": self.key,
            "site": self._site,
            "page_path": self._page_path,
            "user_id": user_id,
        })

    def delete(self, user_id: str = "") -> None:
        """Remove from storage and emit page.deleted."""
        self._storage.delete(self.key)
        self._raw_content = None
        self._emit(EventType.PAGE_DELETED, {
            "key": self.key,
            "site": self._site,
            "page_path": self._page_path,
            "user_id": user_id,
        })


# ── OnWriteEventHandler ───────────────────────────────────────────────────────

_ON_WRITE_PATTERN = re.compile(r"^trigger:\s*on_write", re.MULTILINE)


class OnWriteEventHandler(EventHandler):
    """Handles page.written events by dispatching on_write agents to a queue.

    For each page.written event the handler:
    1. Lists .agents/ under the written page directory in storage.
    2. Reads every agent.md that declares trigger: on_write.
    3. Calls enqueue(agent_md_key, content_key, user_id) for each match.

    The enqueue callable is injected by the caller so this class remains
    independent of SQS or any specific queue implementation.
    """

    def __init__(
        self,
        storage: StorageBackend,
        enqueue: Callable[[str, str, str], None],
    ) -> None:
        self._storage = storage
        self._enqueue = enqueue

    def handle(self, event: Event) -> None:
        if event.type != EventType.PAGE_WRITTEN:
            return
        content_key: str = event.payload.get("key", "")
        if not content_key.endswith("/content.md"):
            return

        page_dir = content_key[: -len("/content.md")]
        agents_prefix = f"{page_dir}/.agents/"
        user_id: str = event.payload.get("user_id", "")

        for agent_key in self._storage.list(agents_prefix):
            if not agent_key.endswith("/agent.md"):
                continue
            text = self._storage.read(agent_key) or ""
            if not _ON_WRITE_PATTERN.search(text):
                continue
            self._enqueue(agent_key, content_key, user_id)
