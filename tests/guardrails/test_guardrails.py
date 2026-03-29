#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""
Integration tests: backend guardrails (test plan sections 1–4).

Covers:
  1. Path Safety (SAFE_PATH) — GET /content and PUT /content reject unsafe paths
  2. JWT Authentication + Site Ownership — missing/expired/wrong-site JWT → 401/403
  3. Page Visibility / Access Control — public/private/shared visibility enforcement

Usage:
    API_BASE_URL=http://localhost:8000 \\
    USER_JWT=eyJ... \\
    USER_SITE=my-site \\
    uv run tests/guardrails/test_guardrails.py

    # With cross-user tests:
    API_BASE_URL=http://localhost:8000 \\
    USER_JWT=eyJ... \\
    USER_SITE=my-site \\
    USER_B_JWT=eyJ... \\
    USER_B_EMAIL=b@example.com \\
    uv run tests/guardrails/test_guardrails.py

Required env vars:
    API_BASE_URL   Backend root URL, e.g. http://localhost:8000
    USER_JWT       Valid JWT for the primary test user (user A)
    USER_SITE      Site name owned by user A

Optional env vars:
    USER_B_JWT     Valid JWT for a second user (enables cross-site ownership tests)
    USER_B_EMAIL   Email address of user B (enables shared-write tests)
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

API_BASE_URL = os.environ.get("API_BASE_URL", "").rstrip("/")
USER_JWT     = os.environ.get("USER_JWT", "")
USER_SITE    = os.environ.get("USER_SITE", "")
USER_B_JWT   = os.environ.get("USER_B_JWT", "")
USER_B_EMAIL = os.environ.get("USER_B_EMAIL", "")

if not API_BASE_URL:
    print("ERROR: API_BASE_URL is required")
    sys.exit(1)
if not USER_JWT:
    print("ERROR: USER_JWT is required")
    sys.exit(1)
if not USER_SITE:
    print("ERROR: USER_SITE is required")
    sys.exit(1)

AUTH_A = {"Authorization": f"Bearer {USER_JWT}"}
AUTH_B = {"Authorization": f"Bearer {USER_B_JWT}"} if USER_B_JWT else {}

_RUN_ID = uuid.uuid4().hex[:8]
_CREATED_PAGES: list[str] = []  # page paths (not file paths) to clean up


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_content(path: str, headers: dict = None) -> httpx.Response:
    return httpx.get(
        f"{API_BASE_URL}/content",
        params={"site": USER_SITE, "path": path},
        headers=headers or {},
        timeout=15,
    )


def put_content(path: str, body: str, headers: dict = None) -> httpx.Response:
    return httpx.put(
        f"{API_BASE_URL}/content",
        params={"site": USER_SITE, "path": path},
        content=body.encode(),
        headers={"Content-Type": "text/plain", **(headers or AUTH_A)},
        timeout=15,
    )


def put_settings(page_path: str, visibility: str, shared_with: list = None) -> httpx.Response:
    return httpx.put(
        f"{API_BASE_URL}/settings",
        params={"site": USER_SITE, "path": f"{page_path}/content.md" if page_path else "content.md"},
        json={"visibility": visibility, "shared_with": shared_with or []},
        headers={"Content-Type": "application/json", **AUTH_A},
        timeout=15,
    )


def page(suffix: str) -> str:
    """Return a unique page path and register it for cleanup."""
    p = f"test-{_RUN_ID}-{suffix}"
    _CREATED_PAGES.append(p)
    return p


def setup_page(page_path: str, content: str = "# Test") -> None:
    """Create a page via PUT /content.  Exits with an error if auth fails."""
    r = put_content(f"{page_path}/content.md", content)
    if r.status_code in (401, 403):
        print(
            f"\nERROR: setup_page('{page_path}') returned HTTP {r.status_code}. "
            "USER_JWT may be expired — refresh it and re-run.\n"
        )
        sys.exit(1)


# ── Section 1: Path Safety ────────────────────────────────────────────────────

print(f"\nGuardrails Integration Tests  [run={_RUN_ID}]")
print(f"API base : {API_BASE_URL}  site : {USER_SITE}\n")

# ── Pre-flight: verify JWTs are valid ─────────────────────────────────────────
_preflight = httpx.get(
    f"{API_BASE_URL}/my-site",
    headers=AUTH_A,
    timeout=10,
)
if _preflight.status_code == 401:
    print("ERROR: USER_JWT is expired or invalid — refresh it and re-run.")
    sys.exit(1)

if USER_B_JWT:
    _preflight_b = httpx.get(
        f"{API_BASE_URL}/my-site",
        headers=AUTH_B,
        timeout=10,
    )
    if _preflight_b.status_code == 401:
        print("ERROR: USER_B_JWT is expired or invalid — refresh it and re-run.")
        sys.exit(1)

print("1. Path Safety (SAFE_PATH)")

# 1a. Path traversal on GET
r = get_content("../../etc/passwd")
check("GET ../../etc/passwd → 400", r.status_code == 400, f"got {r.status_code}")

# 1b. Internal .mcp path on GET
r = get_content(".mcp/agents/uuid/meta.json")
check("GET .mcp/agents/uuid/meta.json → 400", r.status_code == 400, f"got {r.status_code}")

# 1c. Archive path on GET
r = get_content(".archive/old-page/content.md")
check("GET .archive/old-page/content.md → 400", r.status_code == 400, f"got {r.status_code}")

# 1d. Path traversal on PUT
r = put_content("../../etc/passwd", "bad")
check("PUT ../../etc/passwd → 400", r.status_code == 400, f"got {r.status_code}")

# 1e. Internal .mcp path on PUT
r = put_content(".mcp/agents/uuid/meta.json", "bad")
check("PUT .mcp/agents/uuid/meta.json → 400", r.status_code == 400, f"got {r.status_code}")

# 1f. Allowed paths all work (authenticated)
for allowed_path in [
    "content.md",
    f"test-{_RUN_ID}-pathcheck/content.md",
    "settings.json",
    f"test-{_RUN_ID}-pathcheck/settings.json",
    f".agents/test-{_RUN_ID}/agent.md",
    ".user/notifications.md",
]:
    r = get_content(allowed_path, headers=AUTH_A)
    # 200 (found) or 404/500 (missing) are both acceptable — just not 400
    check(
        f"GET {allowed_path} → not 400",
        r.status_code != 400,
        f"got {r.status_code}",
    )

# ── Section 2: Name Validation (SAFE_PATH / HTTP layer) ──────────────────────
# The SAFE_PATH regex enforces name character constraints at the HTTP layer.
# Agent-layer validation (chat agent refusing bad names) is in tests/llm/.

print("\n2. Name Validation (SAFE_PATH / HTTP layer)")

# 2a. PUT agent.md with uppercase letters in the agent name → 400
r = put_content(f".agents/MY-AGENT-{_RUN_ID}/agent.md", "# bad")
check(
    "PUT .agents/MY-AGENT-.../agent.md → 400 (uppercase rejected by SAFE_PATH)",
    r.status_code == 400,
    f"got {r.status_code}",
)

# 2b. PUT SKILL.md with uppercase letters in the skill name → 400
r = put_content(f".skills/MySkill-{_RUN_ID}/SKILL.md", "---\ndescription: bad\ntools: []\n---")
check(
    "PUT .skills/MySkill-.../SKILL.md → 400 (uppercase rejected by SAFE_PATH)",
    r.status_code == 400,
    f"got {r.status_code}",
)

# 2c. Valid lowercase agent name is not rejected by SAFE_PATH
r = get_content(f".agents/test-{_RUN_ID}-val/agent.md", headers=AUTH_A)
check(
    f"GET .agents/test-{_RUN_ID}-val/agent.md → not 400 (valid name accepted)",
    r.status_code != 400,
    f"got {r.status_code}",
)

# ── Section 3: JWT Authentication + Site Ownership ───────────────────────────

print("\n3. JWT Authentication + Site Ownership")

# Set up a page to test against
p_auth = page("auth-test")
setup_page(p_auth)

# 3a. POST /chat with no Authorization header → 401
r = httpx.post(
    f"{API_BASE_URL}/chat",
    json={"site": USER_SITE, "file_path": "content.md", "message": "hello", "current_content": "", "history": []},
    headers={"Content-Type": "application/json"},
    timeout=15,
)
check("POST /chat with no auth → 401", r.status_code == 401, f"got {r.status_code}")

# 3b. POST /chat with a garbage token → 401
r = httpx.post(
    f"{API_BASE_URL}/chat",
    json={"site": USER_SITE, "file_path": "content.md", "message": "hello", "current_content": "", "history": []},
    headers={"Content-Type": "application/json", "Authorization": "Bearer garbage-token"},
    timeout=15,
)
check("POST /chat with garbage token → 401", r.status_code == 401, f"got {r.status_code}")

# 3c. POST /chat with user B's JWT targeting user A's site → 403
if not USER_B_JWT:
    skip("POST /chat with wrong-site JWT → 403", "set USER_B_JWT to enable")
else:
    r = httpx.post(
        f"{API_BASE_URL}/chat",
        json={"site": USER_SITE, "file_path": "content.md", "message": "hello", "current_content": "", "history": []},
        headers={"Content-Type": "application/json", **AUTH_B},
        timeout=15,
    )
    check("POST /chat with wrong-site JWT → 403", r.status_code == 403, f"got {r.status_code}")

# 3d. GET /content with no auth on a private page → 403
p_private = page("private-auth")
setup_page(p_private)
put_settings(p_private, "private")
r = get_content(f"{p_private}/content.md")
check("GET private page with no auth → 403", r.status_code == 403, f"got {r.status_code}")

# 3e. GET /content with valid auth on a private page → 200
r = get_content(f"{p_private}/content.md", headers=AUTH_A)
check("GET private page with owner JWT → 200", r.status_code == 200, f"got {r.status_code}")

# ── Section 4: Page Visibility / Access Control ───────────────────────────────

print("\n4. Page Visibility / Access Control")

# 4a. Private page — unauthenticated → 403
p_vis = page("visibility")
setup_page(p_vis, "# Visibility test")
put_settings(p_vis, "private")

r = get_content(f"{p_vis}/content.md")
check("private page: no auth → 403", r.status_code == 403, f"got {r.status_code}")

# 4b. Public page — unauthenticated → 200 + X-Page-Access: view
put_settings(p_vis, "public")
r = get_content(f"{p_vis}/content.md")
check(
    "public page: no auth → 200 with X-Page-Access: view",
    r.status_code == 200 and r.headers.get("x-page-access") == "view",
    f"status={r.status_code} X-Page-Access={r.headers.get('x-page-access')}",
)

# 4c. Public page — owner JWT → X-Page-Access: full-control
r = get_content(f"{p_vis}/content.md", headers=AUTH_A)
check(
    "public page: owner JWT → X-Page-Access: full-control",
    r.status_code == 200 and r.headers.get("x-page-access") == "full-control",
    f"X-Page-Access={r.headers.get('x-page-access')}",
)

# 4d. Shared-write user can PUT content.md, but not settings.json
if not USER_B_JWT or not USER_B_EMAIL:
    skip("shared-write user can PUT content.md", "set USER_B_JWT and USER_B_EMAIL to enable")
    skip("shared-write user cannot PUT settings.json → 403", "set USER_B_JWT and USER_B_EMAIL to enable")
    skip("shared user cannot POST /chat → 403", "set USER_B_JWT and USER_B_EMAIL to enable")
else:
    p_shared = page("shared-write")
    setup_page(p_shared, "# Shared write test")
    put_settings(p_shared, "shared", [{"email": USER_B_EMAIL, "access": "write"}])

    r = httpx.put(
        f"{API_BASE_URL}/content",
        params={"site": USER_SITE, "path": f"{p_shared}/content.md"},
        content=b"# Updated by shared user",
        headers={"Content-Type": "text/plain", **AUTH_B},
        timeout=15,
    )
    check(
        "shared-write user can PUT content.md → 200",
        r.status_code == 200,
        f"got {r.status_code}: {r.text[:100]}",
    )

    r = httpx.put(
        f"{API_BASE_URL}/content",
        params={"site": USER_SITE, "path": f"{p_shared}/settings.json"},
        content=b'{"visibility":"public","shared_with":[]}',
        headers={"Content-Type": "text/plain", **AUTH_B},
        timeout=15,
    )
    check(
        "shared-write user cannot PUT settings.json → 403",
        r.status_code == 403,
        f"got {r.status_code}",
    )

    r = httpx.post(
        f"{API_BASE_URL}/chat",
        json={"site": USER_SITE, "file_path": f"{p_shared}/content.md", "message": "hello", "current_content": "", "history": []},
        headers={"Content-Type": "application/json", **AUTH_B},
        timeout=15,
    )
    check(
        "shared user cannot POST /chat → 403",
        r.status_code == 403,
        f"got {r.status_code}",
    )

# ── Cleanup ───────────────────────────────────────────────────────────────────

print("\n── Cleanup ──────────────────────────────────────────────────────────────")
for p in reversed(_CREATED_PAGES):
    r = httpx.delete(
        f"{API_BASE_URL}/content",
        params={"site": USER_SITE, "path": f"{p}/content.md"},
        headers=AUTH_A,
        timeout=15,
    ) if False else None  # No DELETE endpoint — just PUT empty content as a no-op marker
    # Backend has no DELETE /content; pages persist but are cheap S3 objects.
    # Run a cleanup script or use the MCP wiki_delete tool to remove test pages.
    print(f"  note: {p} (no DELETE endpoint — clean up manually or via wiki_delete)")

# ── Summary ───────────────────────────────────────────────────────────────────

total   = len(_results)
passed  = sum(1 for r in _results if r is True)
skipped = sum(1 for r in _results if r is None)
failed  = total - passed - skipped

print(f"\n{'─' * 52}")
print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")

if failed:
    sys.exit(1)
