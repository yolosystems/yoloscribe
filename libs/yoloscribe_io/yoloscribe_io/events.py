from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EventHandler(ABC):
    @abstractmethod
    def handle(self, event: Event) -> None: ...


class LoggerEventHandler(EventHandler):
    def __init__(self, log: logging.Logger | None = None) -> None:
        self._log = log or logger

    def handle(self, event: Event) -> None:
        self._log.info("event type=%s payload=%r", event.type, event.payload)


class EventEmitter:
    """Mixin providing event registration and emission. Subclass and call
    super().__init__() to inherit a default LoggerEventHandler."""

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = [LoggerEventHandler()]

    def add_handler(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def remove_handler(self, handler: EventHandler) -> None:
        self._handlers.remove(handler)

    def _emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        event = Event(type=event_type, payload=payload or {})
        for handler in list(self._handlers):
            try:
                handler.handle(event)
            except Exception:
                logger.exception("handler %r raised on event %s", handler, event_type)


class EventType:
    # Page
    PAGE_READ = "page.read"
    PAGE_WRITTEN = "page.written"
    PAGE_CREATED = "page.created"
    PAGE_DELETED = "page.deleted"
    PAGE_SHARED = "page.shared"
    PAGE_UNSHARED = "page.unshared"
    PAGE_ACCESS_CHANGED = "page.access_changed"
    PAGE_VISIBILITY_CHANGED = "page.visibility_changed"
    PAGE_MEDIA_UPLOADED = "page.media_uploaded"
    PAGE_MEDIA_DELETED = "page.media_deleted"

    # Agent
    AGENT_CREATED = "agent.created"
    AGENT_UPDATED = "agent.updated"
    AGENT_DELETED = "agent.deleted"
    AGENT_SUCCESS = "agent.success"
    AGENT_FAILURE = "agent.failure"
    AGENT_CONFIRM_CONTENT = "agent.confirm_content"

    # Skill
    SKILL_CREATED = "skill.created"
    SKILL_UPDATED = "skill.updated"

    # Settings
    SETTINGS_CHANGED = "settings.changed"

    # Tool auth
    TOOL_AUTH_STARTED = "tool.auth_started"
    TOOL_AUTH_COMPLETED = "tool.auth_completed"
    TOOL_AUTH_FAILED = "tool.auth_failed"
    TOOL_AUTH_EXPIRED = "tool.auth_expired"

    # Webhook
    WEBHOOK_RECEIVED = "webhook.received"

    # Token
    TOKEN_REFRESHED = "token.refreshed"
    TOKEN_EXPIRED = "token.expired"

    # Access
    ACCESS_REQUESTED = "access.requested"
