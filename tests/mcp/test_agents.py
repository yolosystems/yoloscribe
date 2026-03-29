#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""
Integration tests: MCP agent session management.

Covers test plan section 9 from the MCP server spec.

Usage:
    MCP_BASE_URL=https://<domain>/mcp/v1 \\
    USER_JWT=eyJ... \\
    uv run tests/mcp/test_agents.py

Required env vars:
    MCP_BASE_URL   Full URL to the MCP server, e.g. https://your-domain/mcp/v1
    USER_JWT       Valid JWT for the test user
"""

import json
import os
import sys
import uuid

import httpx

# ── Colours ───────────────────────────────────────────────────────────────────

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"

_results: list[bool | None] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = PASS if passed else FAIL
    print(f"  [{status}] {name}")
    if not passed and detail:
        print(f"         {detail}")
    _results.append(passed)


def skip(name: str, reason: str) -> None:
    print(f"  [{SKIP}] {name}")
    print(f"         {reason}")
    _results.append(None)


# ── Config ────────────────────────────────────────────────────────────────────

MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "").rstrip("/") + "/"
USER_JWT     = os.environ.get("USER_JWT", "")

if not MCP_BASE_URL.rstrip("/"):
    print("ERROR: MCP_BASE_URL is required")
    sys.exit(1)

if not USER_JWT:
    print("ERROR: USER_JWT is required")
    sys.exit(1)

_BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Authorization": f"Bearer {USER_JWT}",
}

_RUN_ID = uuid.uuid4().hex[:8]


# ── MCP helpers ───────────────────────────────────────────────────────────────

def _init_session() -> str:
    r = httpx.post(
        MCP_BASE_URL,
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test_agents", "version": "0.1"},
            },
            "id": 0,
        },
        headers=_BASE_HEADERS,
        timeout=15,
    )
    if r.status_code != 200:
        print(f"ERROR: MCP initialize failed: HTTP {r.status_code}: {r.text[:200]}")
        sys.exit(1)
    session_id = r.headers.get("mcp-session-id", "")
    if not session_id:
        print("ERROR: MCP initialize response missing Mcp-Session-Id header")
        sys.exit(1)
    return session_id


_SESSION_ID = _init_session()
AUTH_HEADERS = {**_BASE_HEADERS, "Mcp-Session-Id": _SESSION_ID}


def _parse_response(r: httpx.Response) -> dict:
    ct = r.headers.get("content-type", "")
    if "text/event-stream" in ct:
        for line in r.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ValueError(f"No data line in SSE response: {r.text[:200]}")
    return r.json()


def call_ok(method: str, params: dict) -> tuple[bool, dict | str]:
    r = httpx.post(
        MCP_BASE_URL,
        json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": method, "arguments": params}, "id": 1},
        headers=AUTH_HEADERS,
        timeout=30,
    )
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    body = _parse_response(r)
    if "error" in body:
        return False, f"JSON-RPC error: {body['error']}"
    result = body.get("result", {})
    if isinstance(result, dict) and "content" in result:
        if result.get("isError"):
            return False, result
        try:
            return True, json.loads(result["content"][0]["text"])
        except Exception:
            return True, result
    return True, result


# ── Section 9: agent session management ──────────────────────────────────────

print(f"\nMCP Agent Session Tests  [run={_RUN_ID}]")
print(f"MCP endpoint : {MCP_BASE_URL}\n")

print("9. agent session management")

# 9a. agent_create → agent_id returned, status is active
ok, result = call_ok("agent_create", {
    "agent_name": f"test-agent-{_RUN_ID}",
    "description": "Created by test_agents.py",
    "config": {"test_run": _RUN_ID},
})
check(
    "agent_create → agent_id returned and status is active",
    ok and isinstance(result, dict) and result.get("status") == "active" and "agent_id" in result,
    str(result),
)
agent_id = result.get("agent_id", "") if ok and isinstance(result, dict) else ""

# 9b. agent_get_status → fields present
if not agent_id:
    skip("agent_get_status → fields present", "agent_create failed")
else:
    ok, result = call_ok("agent_get_status", {"agent_id": agent_id})
    check(
        "agent_get_status → required fields present",
        ok and isinstance(result, dict) and all(k in result for k in ("agent_id", "name", "status", "created_at", "last_activity")),
        str(result),
    )

# 9c. agent_update_context → stores arbitrary JSON
if not agent_id:
    skip("agent_update_context → context stored", "agent_create failed")
else:
    test_context = {"step": 1, "data": {"key": "value"}, "run": _RUN_ID}
    ok, result = call_ok("agent_update_context", {"agent_id": agent_id, "context": test_context})
    check(
        "agent_update_context → success with context_id",
        ok and isinstance(result, dict) and "context_id" in result,
        str(result),
    )

# 9d. agent_get_context → returns the same JSON that was stored
if not agent_id:
    skip("agent_get_context → returns stored context", "agent_create failed")
else:
    ok, result = call_ok("agent_get_context", {"agent_id": agent_id})
    retrieved = result.get("context", {}) if isinstance(result, dict) else {}
    check(
        "agent_get_context → returns stored context",
        ok and retrieved == test_context,
        f"expected: {test_context}\ngot: {retrieved}",
    )

# 9e. agent_list → created agent appears
ok, result = call_ok("agent_list", {})
agents = result.get("agents", []) if isinstance(result, dict) else []
agent_ids = [a.get("agent_id") for a in agents]
check(
    "agent_list → created agent appears in results",
    ok and agent_id in agent_ids,
    f"agent_id={agent_id!r} not found in: {agent_ids}",
)

# 9f. agent_list entries have required fields
if agents:
    sample = agents[0]
    check(
        "agent_list entries have agent_id, name, status, last_activity",
        all(k in sample for k in ("agent_id", "name", "status", "last_activity")),
        f"sample: {sample}",
    )
else:
    skip("agent_list entries have agent_id, name, status, last_activity", "no agents returned")

# 9g. agent_get_context on non-existent agent_id → error
ok, result = call_ok("agent_get_context", {"agent_id": f"00000000-0000-0000-0000-{_RUN_ID}"})
check(
    "agent_get_context on non-existent agent_id → error",
    not ok,
    f"expected error but got: {result}",
)

# ── Summary ───────────────────────────────────────────────────────────────────

total   = len(_results)
passed  = sum(1 for r in _results if r is True)
skipped = sum(1 for r in _results if r is None)
failed  = total - passed - skipped

print(f"\n{'─' * 52}")
print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")

if failed:
    sys.exit(1)
