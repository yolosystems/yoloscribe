from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable

from .markdown_file import MarkdownFile
from .storage import StorageBackend


_ON_NOTIFY_PATTERN = re.compile(r"^trigger:\s*on_notify", re.MULTILINE)

# These event types are appended to notifications.md but must never trigger
# on_notify agent dispatch — doing so would create an infinite feedback loop.
NO_DISPATCH_EVENTS: frozenset[str] = frozenset({"agent_success", "agent_failure"})


class NotificationsMarkdownFile(MarkdownFile):
    """Append-only notification log at {site}/.user/notifications.md.

    The notify() method:
    1. Formats and appends a canonical entry to the notification log.
    2. Dispatches to on_notify agents at the site root (unless the event type
       is in NO_DISPATCH_EVENTS — loop guard for agent_success / agent_failure).

    Dispatch uses the injected *enqueue* callable so this class has no
    dependency on SQS or any specific queue. Pass enqueue=None to write
    notifications without dispatching (e.g. in tests or local mode).

    enqueue signature: (agent_md_key, notifications_key, prompt, user_id)
    """

    def __init__(
        self,
        site: str,
        storage: StorageBackend,
        enqueue: Callable[[str, str, str, str], None] | None = None,
    ) -> None:
        super().__init__(site, ".user/notifications.md", storage)
        self._enqueue = enqueue

    def notify(
        self,
        event_type: str,
        payload: dict[str, str],
        user_id: str = "",
    ) -> None:
        """Append an entry and dispatch to on_notify agents.

        payload values are coerced to strings so callers don't need to
        pre-format them.
        """
        entry = _format_entry(event_type, payload)

        # Always re-read before writing to handle concurrent appends safely.
        existing = self._storage.read(self.key) or ""
        updated = existing + entry
        self._storage.write(self.key, updated)
        self._raw_content = updated

        if event_type not in NO_DISPATCH_EVENTS:
            self._dispatch(entry, user_id)

    def _dispatch(self, entry: str, user_id: str) -> None:
        if self._enqueue is None:
            return

        agents_prefix = f"{self._site}/.agents/"
        prompt = (
            "A new notification has been added to notifications.md:\n\n"
            f"{entry.strip()}\n\n"
            "Process this notification according to your instructions."
        )

        for agent_key in self._storage.list(agents_prefix):
            if not agent_key.endswith("/agent.md"):
                continue
            text = self._storage.read(agent_key) or ""
            if not _ON_NOTIFY_PATTERN.search(text):
                continue
            self._enqueue(agent_key, self.key, prompt, user_id)


# ── Entry formatting ──────────────────────────────────────────────────────────

def _format_entry(event_type: str, payload: dict[str, str]) -> str:
    """Build a canonical notification entry string.

    Format:
        ## YYYY-MM-DD HH:MM UTC — {event_type}

        key: value
        ...

    """
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"## {ts} — {event_type}", ""]
    for key, value in payload.items():
        lines.append(f"{key}: {value}")
    lines.append("")
    return "\n".join(lines) + "\n"
