"""Notification helpers — write canonical entries to a site's notifications.md."""

from __future__ import annotations

import datetime

from s3_helpers import get_content, put_content

NOTIFICATIONS_PATH = ".user/notifications.md"

# These event types are written to notifications.md but must never trigger
# on_notify dispatch — they would cause an agent feedback loop.
NO_DISPATCH_EVENTS = frozenset({"agent_success", "agent_failure"})


def write_notification(
    site: str,
    event_type: str,
    payload: dict[str, str],
    *,
    user_id: str = "",
) -> None:
    """Append a canonical notification entry to the site's notifications.md.

    Entry format:
        ## YYYY-MM-DD HH:MM UTC — {event_type}

        key: value
        ...

    agent_success and agent_failure events are written but never enqueue
    on_notify agents (loop guard). on_notify dispatch will be wired here
    in YOL-224.
    """
    ts = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"## {ts} — {event_type}", ""]
    for key, value in payload.items():
        lines.append(f"{key}: {value}")
    lines.append("")
    entry = "\n".join(lines) + "\n"

    existing = get_content(site, NOTIFICATIONS_PATH)
    put_content(site, NOTIFICATIONS_PATH, existing + entry)
