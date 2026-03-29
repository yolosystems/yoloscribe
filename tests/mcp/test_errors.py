#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""
Integration tests: MCP auth scoping and error handling.

Covers test plan sections 10–11 from the MCP server spec.

Section 10 (auth scoping) requires USER_B_JWT to test cross-user isolation.
Section 11 (error handling) requires only MCP access.

Usage:
    MCP_BASE_URL=https://<domain>/mcp/v1 \\
    USER_JWT=eyJ... \\
    uv run tests/mcp/test_errors.py

    # With cross-user scoping tests:
    MCP_BASE_URL=https://<domain>/mcp/v1 \\
    USER_JWT=eyJ... \\
    USER_B_JWT=eyJ... \\
    uv run tests/mcp/test_errors.py

Required env vars:
    MCP_BASE_URL   Full URL to the MCP server
    USER_JWT       Valid JWT for the primary test user

Optional env vars:
    USER_B_JWT     Valid JWT for a second user (enables cross-user scoping tests)
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
USER_B_JWT   = os.environ.get("USER_B_JWT", "")

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
_CREATED_PATHS: list[str] = []


# ── MCP helpers ───────────────────────────────────────────────────────────────

def _init_session(jwt: str) -> str:
    r = httpx.post(
        MCP_BASE_URL,
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test_errors", "version": "0.1"},
            },
            "id": 0,
        },
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {jwt}",
        },
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


_SESSION_A = _init_session(USER_JWT)
AUTH_HEADERS_A = {**_BASE_HEADERS, "Mcp-Session-Id": _SESSION_A}

_SESSION_B = _init_session(USER_B_JWT) if USER_B_JWT else ""
AUTH_HEADERS_B = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Authorization": f"Bearer {USER_B_JWT}",
    "Mcp-Session-Id": _SESSION_B,
} if USER_B_JWT else {}


def _parse_response(r: httpx.Response) -> dict:
    ct = r.headers.get("content-type", "")
    if "text/event-stream" in ct:
        for line in r.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ValueError(f"No data line in SSE response: {r.text[:200]}")
    return r.json()


def call_ok(method: str, params: dict, headers: dict = None) -> tuple[bool, dict | str]:
    r = httpx.post(
        MCP_BASE_URL,
        json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": method, "arguments": params}, "id": 1},
        headers=headers or AUTH_HEADERS_A,
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


def page_path(suffix: str) -> str:
    p = f"test-{_RUN_ID}-{suffix}"
    _CREATED_PATHS.append(p)
    return p


# ── Section 10: auth scoping ──────────────────────────────────────────────────

print(f"\nMCP Auth Scoping & Error Handling Tests  [run={_RUN_ID}]")
print(f"MCP endpoint : {MCP_BASE_URL}\n")

print("10. auth scoping")

# 10a. wiki_create writes only to the authenticated user's own site.
# We verify this structurally: user A creates a page and can read it back,
# while user B (different session/site) cannot see it in their own wiki_list.
p_scoped = page_path("scoped")
call_ok("wiki_create", {"page_path": p_scoped, "content": "# Scoped page"})

if not USER_B_JWT:
    skip(
        "user B cannot see user A's pages in wiki_list",
        "set USER_B_JWT to enable cross-user scoping tests",
    )
    skip(
        "user B cannot read user A's page via wiki_read",
        "set USER_B_JWT to enable cross-user scoping tests",
    )
    skip(
        "agent_list returns only the authenticated user's sessions",
        "set USER_B_JWT to enable cross-user scoping tests",
    )
else:
    # User B lists pages — should not see user A's page
    ok, result = call_ok("wiki_list", {}, headers=AUTH_HEADERS_B)
    pages_b = result.get("pages", []) if isinstance(result, dict) else []
    paths_b = {p["path"] for p in pages_b}
    check(
        "user B cannot see user A's pages in wiki_list",
        ok and p_scoped not in paths_b,
        f"user B's wiki_list unexpectedly contained: {p_scoped}",
    )

    # User B attempts to read user A's page directly — should get not found
    # (MCP tools are scoped to the caller's own site, so the key doesn't exist
    # in user B's site prefix)
    ok, result = call_ok("wiki_read", {"page_path": p_scoped}, headers=AUTH_HEADERS_B)
    check(
        "user B cannot read user A's page via wiki_read",
        not ok,
        f"expected error but got: {result}",
    )

    # Create an agent session as user A, then check user B's agent_list doesn't include it
    ok_a, agent_result = call_ok("agent_create", {"agent_name": f"scoping-test-{_RUN_ID}"})
    agent_id_a = agent_result.get("agent_id", "") if ok_a and isinstance(agent_result, dict) else ""

    ok, result = call_ok("agent_list", {}, headers=AUTH_HEADERS_B)
    agents_b = result.get("agents", []) if isinstance(result, dict) else []
    agent_ids_b = {a.get("agent_id") for a in agents_b}
    check(
        "agent_list returns only the authenticated user's sessions",
        ok and agent_id_a not in agent_ids_b,
        f"user B's agent_list unexpectedly contained user A's agent: {agent_id_a}",
    )

# ── Section 11: error handling ────────────────────────────────────────────────

print("\n11. error handling")

# 11a. Malformed JSON → HTTP 400
r = httpx.post(
    MCP_BASE_URL,
    content=b"this is not json{{{",
    headers=AUTH_HEADERS_A,
    timeout=15,
)
check(
    "malformed JSON body → HTTP 400",
    r.status_code == 400,
    f"got HTTP {r.status_code}",
)

# 11b. Oversized payload → HTTP 413
# Send a body well over any reasonable limit (5 MB of zeros)
big_body = json.dumps({
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {"name": "wiki_create", "arguments": {"page_path": "x", "content": "x" * 5_000_000}},
    "id": 1,
}).encode()
r = httpx.post(
    MCP_BASE_URL,
    content=big_body,
    headers=AUTH_HEADERS_A,
    timeout=30,
)
check(
    "oversized payload → HTTP 413",
    r.status_code == 413,
    f"got HTTP {r.status_code}",
)

# 11c. Server error response body is safe — no stack traces or internal paths.
# Trigger a tool error and verify the response doesn't leak internals.
ok, result = call_ok("wiki_read", {"page_path": f"test-{_RUN_ID}-nonexistent"})
error_text = str(result)
check(
    "tool error response contains no stack trace or internal paths",
    not ok and "Traceback" not in error_text and "/home/" not in error_text and "/app/" not in error_text,
    f"potentially leaking internals in: {error_text[:300]}",
)

# ── Cleanup ───────────────────────────────────────────────────────────────────

print("\n── Cleanup ──────────────────────────────────────────────────────────────")
for pg in reversed(_CREATED_PATHS):
    ok, _ = call_ok("wiki_delete", {"page_path": pg})
    print(f"  {'deleted' if ok else 'FAILED to delete'}: {pg}")

# ── Summary ───────────────────────────────────────────────────────────────────

total   = len(_results)
passed  = sum(1 for r in _results if r is True)
skipped = sum(1 for r in _results if r is None)
failed  = total - passed - skipped

print(f"\n{'─' * 52}")
print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")

if failed:
    sys.exit(1)
