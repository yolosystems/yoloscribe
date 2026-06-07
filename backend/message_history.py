"""In-memory conversation history cache for messaging channels.

Keyed by (user_id, platform, channel_id). Expires entries after 30 minutes of
inactivity and keeps at most 20 turns per channel. No database — history is
intentionally ephemeral. See YOL-339 for the rationale.
"""

from __future__ import annotations

import threading
import time
from collections import deque

_INACTIVITY_TTL: float = 30 * 60  # seconds
_MAX_TURNS = 20  # user+assistant pairs


class _Entry:
    __slots__ = ("turns", "last_used")

    def __init__(self) -> None:
        # Each item is (role, content); deque enforces the per-channel cap.
        self.turns: deque[tuple[str, str]] = deque(maxlen=_MAX_TURNS * 2)
        self.last_used: float = time.monotonic()


class MessageHistoryCache:
    def __init__(
        self,
        ttl: float = _INACTIVITY_TTL,
        max_turns: int = _MAX_TURNS,
    ) -> None:
        self._ttl = ttl
        self._maxlen = max_turns * 2
        self._cache: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(user_id: str, platform: str, channel_id: str) -> str:
        return f"{user_id}:{platform}:{channel_id}"

    def get(self, user_id: str, platform: str, channel_id: str) -> list[dict[str, str]]:
        """Return stored turns as a list of {role, content} dicts."""
        k = self._key(user_id, platform, channel_id)
        with self._lock:
            self._evict()
            entry = self._cache.get(k)
            if entry is None:
                return []
            entry.last_used = time.monotonic()
            return [{"role": role, "content": content} for role, content in entry.turns]

    def append(
        self,
        user_id: str,
        platform: str,
        channel_id: str,
        user_msg: str,
        assistant_reply: str,
    ) -> None:
        """Append a completed user+assistant turn."""
        k = self._key(user_id, platform, channel_id)
        with self._lock:
            if k not in self._cache:
                self._cache[k] = _Entry()
                self._cache[k].turns = deque(maxlen=self._maxlen)
            entry = self._cache[k]
            entry.turns.append(("user", user_msg))
            entry.turns.append(("assistant", assistant_reply))
            entry.last_used = time.monotonic()

    def _evict(self) -> None:
        """Remove stale entries. Must be called while holding self._lock."""
        cutoff = time.monotonic() - self._ttl
        stale = [k for k, v in self._cache.items() if v.last_used < cutoff]
        for k in stale:
            del self._cache[k]


_cache = MessageHistoryCache()


def get_history(user_id: str, platform: str, channel_id: str) -> list[dict[str, str]]:
    return _cache.get(user_id, platform, channel_id)


def append_history(
    user_id: str,
    platform: str,
    channel_id: str,
    user_msg: str,
    assistant_reply: str,
) -> None:
    _cache.append(user_id, platform, channel_id, user_msg, assistant_reply)
