"""dynamodb-local helpers for the R3b single-flight lock test.

Re-exports the polling worker's own `_acquire_page_lock` so R3b exercises the
real lock primitive rather than a re-implementation of it.
"""
from __future__ import annotations

from agent_runner.polling_worker import DDB_AGENT_LOCKS_TABLE, _acquire_page_lock

from .scratch_site import dynamodb_client

__all__ = ["DDB_AGENT_LOCKS_TABLE", "acquire_page_lock", "dynamodb_client"]


def acquire_page_lock(ddb, user_id: str, content_key: str) -> bool:
    return _acquire_page_lock(ddb, user_id, content_key)
