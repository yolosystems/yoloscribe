#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""
Integration tests: MCP server authentication.

Tests:
  1. Missing Authorization header → 401
  2. Invalid bearer token → 401
  3. Malformed auth scheme → 401
  4. Cross-user read isolation: user B's JWT cannot read user A's private page → 403

Usage:
    MCP_BASE_URL=https://<domain>/mcp/v1 \\
    USER_A_JWT=eyJ... \\
    uv run tests/mcp/test_auth.py

    # With cross-user test (requires a second account):
    MCP_BASE_URL=https://<domain>/mcp/v1 \\
    USER_A_JWT=eyJ... \\
    USER_B_JWT=eyJ... \\
    USER_A_SITE=alice-site \\
    uv run tests/mcp/test_auth.py

Required env vars:
    MCP_BASE_URL   Full URL to the MCP server, e.g. https://your-domain/mcp/v1

Optional env vars:
    USER_A_JWT     Valid JWT for the primary test user (skips auth tests if absent)
    USER_B_JWT     Valid JWT for a second user (enables cross-user test)
    USER_A_SITE    Site name owned by user A (enables cross-user test)
    API_BASE_URL   Backend API root; defaults to MCP_BASE_URL with /mcp/v1 stripped
"""

import os
import sys

import httpx

# ── Colours ──────────────────────────────────────────────────────────────────

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


# ── Config ───────────────────────────────────────────────────────────────────

MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "").rstrip("/")
USER_A_JWT   = os.environ.get("USER_A_JWT", "")
USER_B_JWT   = os.environ.get("USER_B_JWT", "")
USER_A_SITE  = os.environ.get("USER_A_SITE", "")
API_BASE_URL = os.environ.get("API_BASE_URL", MCP_BASE_URL.removesuffix("/mcp/v1"))

if not MCP_BASE_URL:
    print("ERROR: MCP_BASE_URL is required")
    sys.exit(1)

# ── Helpers ───────────────────────────────────────────────────────────────────

# A minimal MCP tools/list request — the lightest call we can make.
# Auth is checked before the body is parsed, so the response code is reliable.
MCP_TOOLS_LIST = {"jsonrpc": "2.0", "method": "tools/list", "id": 1}


def mcp_post(headers: dict) -> httpx.Response:
    return httpx.post(
        MCP_BASE_URL,
        json=MCP_TOOLS_LIST,
        headers={"Content-Type": "application/json", **headers},
        timeout=15,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

print(f"\nMCP Auth Integration Tests")
print(f"MCP endpoint : {MCP_BASE_URL}")
print(f"API base     : {API_BASE_URL}\n")

# 1. Missing Authorization header
print("1. Missing Authorization header")
r = mcp_post({})
check("no auth header → 401", r.status_code == 401, f"got HTTP {r.status_code}")

# 2. Invalid bearer token
print("\n2. Invalid bearer token")
r = mcp_post({"Authorization": "Bearer this-is-not-a-valid-token"})
check("garbage bearer token → 401", r.status_code == 401, f"got HTTP {r.status_code}")

# 3. Malformed auth scheme
print("\n3. Malformed auth scheme")
r = mcp_post({"Authorization": "NotBearer abc123"})
check("non-Bearer scheme → 401", r.status_code == 401, f"got HTTP {r.status_code}")

# 4. Cross-user read isolation
print("\n4. Cross-user read isolation")
if not USER_B_JWT or not USER_A_SITE:
    skip(
        "user B cannot read user A's private page → 403",
        "set USER_B_JWT and USER_A_SITE to enable this test",
    )
else:
    # Test via GET /content which accepts an explicit site parameter.
    # User A's page must be private (the default) for this to return 403.
    r = httpx.get(
        f"{API_BASE_URL}/content",
        params={"site": USER_A_SITE, "path": "content.md"},
        headers={"Authorization": f"Bearer {USER_B_JWT}"},
        timeout=15,
    )
    check(
        f"user B JWT cannot read {USER_A_SITE}/content.md → 403",
        r.status_code == 403,
        f"got HTTP {r.status_code}"
        + (" — is user A's root page set to 'public'?" if r.status_code == 200 else ""),
    )

# ── Summary ───────────────────────────────────────────────────────────────────

total   = len(_results)
passed  = sum(1 for r in _results if r is True)
skipped = sum(1 for r in _results if r is None)
failed  = total - passed - skipped

print(f"\n{'─' * 44}")
print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")

if failed:
    sys.exit(1)
