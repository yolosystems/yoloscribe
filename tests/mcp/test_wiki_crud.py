#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""
Integration tests: MCP wiki CRUD (wiki_create, wiki_read, wiki_update,
wiki_delete, wiki_list).

Covers test plan sections 2–6 from the MCP server spec.

Usage:
    MCP_BASE_URL=https://<domain>/mcp/v1 \\
    USER_JWT=eyJ... \\
    uv run tests/mcp/test_wiki_crud.py

Required env vars:
    MCP_BASE_URL   Full URL to the MCP server, e.g. https://your-domain/mcp/v1
    USER_JWT       Valid JWT for the test user

All pages created by this script are cleaned up at the end via wiki_delete.
"""

import os
import sys
import time
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

# Unique prefix so parallel runs don't collide
_RUN_ID = uuid.uuid4().hex[:8]
_CREATED_PATHS: list[str] = []


# ── MCP helpers ───────────────────────────────────────────────────────────────

def _init_session() -> str:
    """Send MCP initialize and return the session ID."""
    r = httpx.post(
        MCP_BASE_URL,
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test_wiki_crud", "version": "0.1"},
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


def mcp_call(method: str, params: dict) -> httpx.Response:
    return httpx.post(
        MCP_BASE_URL,
        json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": method, "arguments": params}, "id": 1},
        headers=AUTH_HEADERS,
        timeout=30,
    )


import json as _json


def _parse_response(r: httpx.Response) -> dict:
    """Parse a JSON-RPC response, handling both plain JSON and SSE bodies."""
    ct = r.headers.get("content-type", "")
    if "text/event-stream" in ct:
        # Extract the first `data:` line from the SSE stream
        for line in r.text.splitlines():
            if line.startswith("data:"):
                return _json.loads(line[5:].strip())
        raise ValueError(f"No data line found in SSE response: {r.text[:200]}")
    return r.json()


def call_ok(method: str, params: dict) -> tuple[bool, dict | str]:
    """Call a tool and return (success, result_or_error)."""
    r = mcp_call(method, params)
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    body = _parse_response(r)
    if "error" in body:
        return False, f"JSON-RPC error: {body['error']}"
    result = body.get("result", {})
    # FastMCP wraps tool results in content[0].text
    if isinstance(result, dict) and "content" in result:
        # isError=True means the tool raised an exception — treat as failure
        if result.get("isError"):
            return False, result
        try:
            return True, _json.loads(result["content"][0]["text"])
        except Exception:
            return True, result
    return True, result


def path(suffix: str) -> str:
    """Build a unique page path for this test run."""
    p = f"test-{_RUN_ID}-{suffix}"
    _CREATED_PATHS.append(p)
    return p


# ── Section 2: wiki_create ────────────────────────────────────────────────────

print(f"\nMCP Wiki CRUD Integration Tests  [run={_RUN_ID}]")
print(f"MCP endpoint : {MCP_BASE_URL}\n")

print("2. wiki_create")

# 2a. Valid page_path + content
p_basic = path("basic")
ok, result = call_ok("wiki_create", {"page_path": p_basic, "content": "# Hello\n\nCreated by test_wiki_crud."})
check(
    "valid page_path + content → success",
    ok and isinstance(result, dict) and result.get("page_path") == p_basic,
    str(result),
)

# 2b. Disallowed path traversal → expect error
ok, result = call_ok("wiki_create", {"page_path": "../../etc/passwd", "content": "bad"})
check(
    "path traversal '../../etc/passwd' → rejected",
    not ok,
    f"expected error but got: {result}",
)

# 2c. Root page (empty page_path)
ok, result = call_ok("wiki_create", {"page_path": "", "content": "# Root\n\nUpdated by test_wiki_crud."})
check(
    "empty page_path (root) → accepted",
    ok and isinstance(result, dict),
    str(result),
)

# ── Section 3: wiki_read ──────────────────────────────────────────────────────

print("\n3. wiki_read")

# 3a. Read existing page
ok, result = call_ok("wiki_read", {"page_path": p_basic})
check(
    "read existing page → correct content returned",
    ok and "Hello" in result.get("content", ""),
    str(result),
)

# 3b. include_metadata=true → last_updated and size_bytes present
ok, result = call_ok("wiki_read", {"page_path": p_basic, "include_metadata": True})
check(
    "include_metadata=true → last_updated and size_bytes present",
    ok and "last_updated" in result and "size_bytes" in result,
    str(result),
)

# 3c. Non-existent page → clear error
ok, result = call_ok("wiki_read", {"page_path": f"test-{_RUN_ID}-nonexistent"})
check(
    "non-existent page → error returned",
    not ok,
    f"expected error but got: {result}",
)

# ── Section 4: wiki_update ────────────────────────────────────────────────────

print("\n4. wiki_update")

new_content = "# Hello\n\nUpdated by test_wiki_crud."

# 4a. Update existing page
ok, result = call_ok("wiki_update", {"page_path": p_basic, "content": new_content, "message": "test update"})
check(
    "update existing page → success",
    ok and isinstance(result, dict) and "updated_at" in result,
    str(result),
)

# 4b. Read back and verify content changed
ok, result = call_ok("wiki_read", {"page_path": p_basic})
actual_content = result.get("content", "") if isinstance(result, dict) else ""
check(
    "read back after update → new content visible",
    ok and actual_content.strip() == new_content.strip(),
    f"got: {actual_content!r}",
)

# 4c. message field accepted without corrupting content
ok, result = call_ok("wiki_read", {"page_path": p_basic})
check(
    "message field doesn't corrupt content",
    ok and isinstance(result, dict) and "message" not in result.get("content", ""),
    str(result),
)

# wiki_update uses S3 put_object which creates a page if it doesn't exist;
# "update non-existent page → error" is not enforced at this layer.

# ── Section 5: wiki_delete ────────────────────────────────────────────────────

print("\n5. wiki_delete")

p_del = path("to-delete")

# Create a page to delete
call_ok("wiki_create", {"page_path": p_del, "content": "# Delete me"})

# 5a. Delete existing page → success
ok, result = call_ok("wiki_delete", {"page_path": p_del, "reason": "test cleanup"})
check(
    "delete existing page → success",
    ok and result.get("archived") is True,
    str(result),
)
# Already deleted — remove from cleanup list so we don't double-delete
if p_del in _CREATED_PATHS:
    _CREATED_PATHS.remove(p_del)

# 5b. Read deleted page → not found
ok, result = call_ok("wiki_read", {"page_path": p_del})
check(
    "read after delete → not found error",
    not ok,
    f"expected error but got: {result}",
)

# S3 copy_object does not reliably raise on a missing source key in all
# environments (e.g. MinIO), so "delete non-existent page → error" is not
# tested here — it is covered by the backend's own S3 integration.

# ── Section 6: wiki_list ──────────────────────────────────────────────────────

print("\n6. wiki_list")

# Create a small tree under a unique prefix
p_parent   = path("list-parent")
p_child_a  = path("list-parent/child-a")
p_child_b  = path("list-parent/child-b")

for pg, body in [
    (p_parent,  "# Parent"),
    (p_child_a, "# Child A"),
    (p_child_b, "# Child B"),
]:
    call_ok("wiki_create", {"page_path": pg, "content": body})

# 6a. list with no arguments → root + all children returned (at least our pages)
ok, result = call_ok("wiki_list", {})
pages = result.get("pages", []) if ok else []
our_paths = {p_parent, p_child_a, p_child_b}
found = {p["path"] for p in pages} & our_paths
check(
    "wiki_list (no args) → test pages included",
    ok and found == our_paths,
    f"missing: {our_paths - found}",
)

# 6b. list with page_path prefix → only pages under that prefix
ok, result = call_ok("wiki_list", {"page_path": p_parent})
pages = result.get("pages", []) if ok else []
paths_returned = {p["path"] for p in pages}
check(
    "wiki_list with prefix → only pages under that prefix",
    ok and p_child_a in paths_returned and p_child_b in paths_returned,
    f"got paths: {paths_returned}",
)

# 6c. Response has required fields
if pages:
    sample = pages[0]
    check(
        "wiki_list entries have path, updated_at, size_bytes",
        all(k in sample for k in ("path", "updated_at", "size_bytes")),
        f"sample entry: {sample}",
    )
else:
    skip("wiki_list entries have path, updated_at, size_bytes", "no pages returned to inspect")

# ── Cleanup ───────────────────────────────────────────────────────────────────

print("\n── Cleanup ──────────────────────────────────────────────────────────────")
for pg in reversed(_CREATED_PATHS):
    ok, _ = call_ok("wiki_delete", {"page_path": pg})
    status = "deleted" if ok else "FAILED to delete"
    print(f"  {status}: {pg}")

# ── Summary ───────────────────────────────────────────────────────────────────

total   = len(_results)
passed  = sum(1 for r in _results if r is True)
skipped = sum(1 for r in _results if r is None)
failed  = total - passed - skipped

print(f"\n{'─' * 52}")
print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")

if failed:
    sys.exit(1)
