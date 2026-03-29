#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""
Integration tests: page-level write locking (test plan groups 3 and 6).

Group 3 — Simulated Race (HTTP If-Match layer):
  - GET /content returns ETag header
  - PUT /content with correct If-Match succeeds (200)
  - ETag changes after a write
  - PUT /content with stale If-Match returns 409
  - 409 response contains a helpful conflict message
  - Content is unchanged after a stale-ETag rejection
  - PUT /content without If-Match still succeeds (unconditional writes)
  - PUT /content with a freshly fetched ETag succeeds

Group 6 — Concurrent Writers (stress test):
  - Two simultaneous POST /chat requests to the same page complete without errors
  - Neither returns a 500
  - Final content is non-empty coherent markdown

Usage:
    API_BASE_URL=http://localhost:8000 \\
    USER_JWT=eyJ... \\
    USER_SITE=my-site \\
    uv run tests/write-lock/test_write_lock.py

Required env vars:
    API_BASE_URL   Backend root URL, e.g. http://localhost:8000
    USER_JWT       Valid JWT for the primary test user
    USER_SITE      Site name owned by user
"""

import os
import sys
import threading
import time
import uuid

import httpx

# ── Colours ───────────────────────────────────────────────────────────────────

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

_results: list[bool] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    status = PASS if passed else FAIL
    print(f"  [{status}] {name}")
    if not passed and detail:
        print(f"         {detail}")
    _results.append(passed)


# ── Config ────────────────────────────────────────────────────────────────────

API_BASE_URL = os.environ.get("API_BASE_URL", "").rstrip("/")
USER_JWT     = os.environ.get("USER_JWT", "")
USER_SITE    = os.environ.get("USER_SITE", "")

if not API_BASE_URL:
    print("ERROR: API_BASE_URL is required")
    sys.exit(1)
if not USER_JWT:
    print("ERROR: USER_JWT is required")
    sys.exit(1)
if not USER_SITE:
    print("ERROR: USER_SITE is required")
    sys.exit(1)

AUTH = {"Authorization": f"Bearer {USER_JWT}"}
_RUN_ID = uuid.uuid4().hex[:8]

# ── Pre-flight ────────────────────────────────────────────────────────────────

print(f"\nWrite Lock Integration Tests  [run={_RUN_ID}]")
print(f"API base : {API_BASE_URL}  site : {USER_SITE}\n")

_preflight = httpx.get(f"{API_BASE_URL}/my-site", headers=AUTH, timeout=10)
if _preflight.status_code == 401:
    print("ERROR: USER_JWT is expired or invalid — refresh it and re-run.")
    sys.exit(1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_content(path: str) -> httpx.Response:
    return httpx.get(
        f"{API_BASE_URL}/content",
        params={"site": USER_SITE, "path": path},
        headers=AUTH,
        timeout=15,
    )


def put_content(path: str, body: str, if_match: str | None = None) -> httpx.Response:
    headers: dict = {"Content-Type": "text/plain", **AUTH}
    if if_match:
        headers["If-Match"] = if_match
    return httpx.put(
        f"{API_BASE_URL}/content",
        params={"site": USER_SITE, "path": path},
        content=body.encode(),
        headers=headers,
        timeout=15,
    )


def chat(message: str, file_path: str) -> httpx.Response:
    return httpx.post(
        f"{API_BASE_URL}/chat",
        json={
            "site": USER_SITE,
            "file_path": file_path,
            "message": message,
            "current_content": "",
            "history": [],
        },
        headers={"Content-Type": "application/json", **AUTH},
        timeout=120,
    )


def setup_page(page_path: str, content: str) -> None:
    r = put_content(f"{page_path}/content.md", content)
    if r.status_code in (401, 403):
        print(
            f"\nERROR: setup_page('{page_path}') returned HTTP {r.status_code}. "
            "USER_JWT may be expired — refresh it and re-run.\n"
        )
        sys.exit(1)


def etag_from(r: httpx.Response) -> str | None:
    return r.headers.get("etag")


# ── Group 3: Simulated Race (HTTP If-Match layer) ─────────────────────────────

print("3. Simulated Race (HTTP If-Match layer)")

p3 = f"test-{_RUN_ID}-write-lock"
setup_page(p3, "# Write Lock Test\n\nOriginal content.")
fp3 = f"{p3}/content.md"

# 3a. GET /content returns ETag header
r = get_content(fp3)
etag_original = etag_from(r)
check(
    "GET /content returns ETag header",
    bool(etag_original),
    f"ETag={etag_original!r}",
)

# 3b. PUT /content with correct If-Match → 200
r = put_content(fp3, "# Write Lock Test\n\nFirst update.", if_match=etag_original)
check(
    "PUT /content with correct If-Match → 200",
    r.status_code == 200,
    f"got {r.status_code}: {r.text[:100]}",
)

# 3c. ETag changes after write
r = get_content(fp3)
etag_v1 = etag_from(r)
check(
    "ETag changes after write",
    bool(etag_v1) and etag_v1 != etag_original,
    f"original={etag_original!r} new={etag_v1!r}",
)

# 3d. PUT /content with stale If-Match → 409
r_stale = put_content(fp3, "# STALE WRITE — should not land", if_match=etag_original)
check(
    "PUT /content with stale If-Match → 409",
    r_stale.status_code == 409,
    f"got {r_stale.status_code}: {r_stale.text[:100]}",
)

# 3e. 409 response body contains a helpful conflict message
conflict_text = r_stale.text.lower()
check(
    "409 response contains conflict detail",
    "conflict" in conflict_text or "modified" in conflict_text or "reload" in conflict_text,
    f"body={r_stale.text[:150]}",
)

# 3f. Content unchanged after stale-ETag rejection
r = get_content(fp3)
check(
    "Content unchanged after stale-ETag rejection",
    "First update" in r.text and "STALE WRITE" not in r.text,
    f"content={r.text[:100]}",
)

# 3g. PUT /content without If-Match still succeeds (unconditional write)
r = put_content(fp3, "# Write Lock Test\n\nUnconditional update.")
check(
    "PUT /content without If-Match → 200 (unconditional write)",
    r.status_code == 200,
    f"got {r.status_code}",
)

# 3h. PUT /content with freshly fetched ETag → 200
r = get_content(fp3)
etag_v2 = etag_from(r)
r = put_content(fp3, "# Write Lock Test\n\nFinal update.", if_match=etag_v2)
check(
    "PUT /content with freshly fetched ETag → 200",
    r.status_code == 200,
    f"got {r.status_code}",
)


# ── Group 6: Concurrent Writers ───────────────────────────────────────────────

print("\n6. Concurrent Writers — Stress Test")

p6 = f"test-{_RUN_ID}-concurrent"
setup_page(
    p6,
    "# Concurrent Write Test\n\n"
    "## Section A\n\nOriginal content A.\n\n"
    "## Section B\n\nOriginal content B.",
)
fp6 = f"{p6}/content.md"

_responses: list[httpx.Response | Exception | None] = [None, None]


def _chat_thread(idx: int, message: str) -> None:
    try:
        _responses[idx] = chat(message, fp6)
    except Exception as exc:
        _responses[idx] = exc


t1 = threading.Thread(
    target=_chat_thread,
    args=(0, "Add a sentence to Section A that says 'Writer 1 was here'"),
)
t2 = threading.Thread(
    target=_chat_thread,
    args=(1, "Add a sentence to Section B that says 'Writer 2 was here'"),
)

t1.start()
time.sleep(0.3)  # small stagger so both requests are in-flight simultaneously
t2.start()

t1.join(timeout=180)
t2.join(timeout=180)

r1, r2 = _responses

# 6a. Neither request raised an unhandled exception
check(
    "concurrent writer 1 — no exception",
    not isinstance(r1, Exception),
    str(r1) if isinstance(r1, Exception) else "",
)
check(
    "concurrent writer 2 — no exception",
    not isinstance(r2, Exception),
    str(r2) if isinstance(r2, Exception) else "",
)

s1 = r1.status_code if isinstance(r1, httpx.Response) else -1
s2 = r2.status_code if isinstance(r2, httpx.Response) else -1

# 6b. Neither request returned a 500
check("concurrent writer 1 — no 500", s1 != 500, f"got HTTP {s1}")
check("concurrent writer 2 — no 500", s2 != 500, f"got HTTP {s2}")

# 6c. Both returned a 200 or a graceful user-facing error (not 5xx)
check(
    "concurrent writer 1 — 200 or user-facing response",
    isinstance(r1, httpx.Response) and s1 < 500,
    f"got HTTP {s1}",
)
check(
    "concurrent writer 2 — 200 or user-facing response",
    isinstance(r2, httpx.Response) and s2 < 500,
    f"got HTTP {s2}",
)

# 6d. Final content is non-empty valid markdown
r_final = get_content(fp6)
final_text = r_final.text.strip()
check(
    "final content is non-empty after concurrent writes",
    len(final_text) > 0,
    f"content length={len(final_text)}",
)
check(
    "final content is valid markdown (starts with #)",
    final_text.startswith("#"),
    f"starts with: {final_text[:60]!r}",
)

# ── Summary ───────────────────────────────────────────────────────────────────

total  = len(_results)
passed = sum(1 for r in _results if r is True)
failed = total - passed

print(f"\n{'─' * 52}")
print(f"Results: {passed} passed, {failed} failed")

if failed:
    sys.exit(1)
