"""R1 — No-bucket (MCP runtime).

Contract §3 / §8: with the runner's OWN direct-S3 access revoked, a full Page
cycle still completes end-to-end — because every read, write, run-log and
notification goes through the YoloScribe MCP with a scoped run token. This is
the *positive* proof that replaces the earlier negative baseline (which merely
showed the pre-MCP runner dying on its first S3 GetObject).

How revocation is done here: the runner subprocess is handed deliberately
invalid AWS credentials and no profile, so ANY S3 call it makes fails loudly
(InvalidAccessKeyId) against the real endpoint. The backend (a separate
process) keeps its own valid credentials and does the actual S3 work on the
runner's behalf. If the runner completes the cycle with *no* S3 error in its
logs, it demonstrably never touched S3 — everything went through the MCP.

Live only. Driven entirely by environment (no secrets are committed); the test
skips when these are unset or the backend is unreachable:

  CONFORMANCE_MCP_BASE     backend base URL, e.g. http://localhost:8000
  CONFORMANCE_MINT_SECRET  the INTERNAL_MINT_SECRET the backend was started with
  CONFORMANCE_SITE         a site to write a throwaway page under (e.g. knuth)
  CONFORMANCE_USER_ID      that site's owner user id
  ANTHROPIC_API_KEY        (any Anthropic key; the run performs one small LLM turn)

It runs a real page-agent turn on Haiku, so it costs a little Anthropic spend.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from yoloscribe_io import AgentDefinition, build_agent_md

from agent_runner.mcp_client import HttpMCPClient

from .support.report import REPORT, RResult

_AGENT_RUNNER_DIR = Path(__file__).resolve().parents[2]
_SCRATCH_PAGE = "scratch/r1-conformance"
_AGENT_NAME = "r1-page-agent"
_SEED = "# R1 Conformance\n\nSeed line (pre-run).\n"


def _env_config() -> dict | None:
    cfg = {
        "base": os.environ.get("CONFORMANCE_MCP_BASE", "").rstrip("/"),
        "mint_secret": os.environ.get("CONFORMANCE_MINT_SECRET", ""),
        "site": os.environ.get("CONFORMANCE_SITE", ""),
        "user_id": os.environ.get("CONFORMANCE_USER_ID", ""),
    }
    if not all(cfg.values()) or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    return cfg


def _mint(cfg: dict) -> tuple[str, str]:
    """Mint a page-scoped run token for the scratch page; return (token, mcp_url)."""
    resp = httpx.post(
        f"{cfg['base']}/internal/runs/mint",
        headers={"X-Internal-Auth": cfg["mint_secret"]},
        json={
            "site": cfg["site"],
            "user_id": cfg["user_id"],
            "agent_name": _AGENT_NAME,
            "agent_type": "page",
            "page_path": _SCRATCH_PAGE,
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    mcp_url = data["mcp_url"]
    if "localhost" not in mcp_url and "127.0.0.1" not in mcp_url:
        mcp_url = f"{cfg['base']}/mcp/v1"  # backend advertised a cluster URL; use the local one
    return data["token"], mcp_url


@pytest.mark.conformance_backend
def test_r1_full_page_cycle_via_mcp():
    cfg = _env_config()
    if cfg is None:
        pytest.skip(
            "live R1 needs CONFORMANCE_MCP_BASE / CONFORMANCE_MINT_SECRET / "
            "CONFORMANCE_SITE / CONFORMANCE_USER_ID + ANTHROPIC_API_KEY (a running backend)"
        )
    try:
        httpx.get(f"{cfg['base']}/health", timeout=5.0)
    except Exception:
        pytest.skip(f"backend at {cfg['base']} unreachable")

    site = cfg["site"]
    agent_md_key = f"{site}/{_SCRATCH_PAGE}/.agents/{_AGENT_NAME}/agent.md"
    content_key = f"{site}/{_SCRATCH_PAGE}/content.md"

    agent_def = AgentDefinition(
        name=_AGENT_NAME,
        trigger="manual",
        type="page",
        model="haiku",
        description=(
            "You maintain this page. When asked to change it, reply with ONLY the full "
            "updated markdown, preserving all existing text. Do NOT call the page_write "
            "or page_read tools — the system reads the current content for you and saves "
            "your reply automatically."
        ),
    )
    agent_md = build_agent_md(agent_def)

    # 1. Seed the scratch page through the MCP (a valid token, standing in for the owner).
    seed_token, mcp_url = _mint(cfg)
    with HttpMCPClient(mcp_url, seed_token) as c:
        c.wiki_write(_SCRATCH_PAGE, _SEED, "R1 conformance seed")

    # 2. Mint the run token the runner will use.
    run_token, _ = _mint(cfg)

    # 3. Launch the runner with its OWN S3 access revoked (invalid creds, no profile)
    #    but full MCP credentials + the agent.md handed in via the env.
    env = {
        **os.environ,
        "BUCKET": "conformance-r1-unused",
        "AGENT_MD_KEY": agent_md_key,
        "AGENT_MD_CONTENT": agent_md,
        "CONTENT_KEY": content_key,
        "AGENT_PROMPT": "Append a new line containing exactly R1-OK to the page.",
        "USER_ID": cfg["user_id"],
        "AWS_REGION": "us-east-1",
        # Revoke the runner's OWN S3: real AWS endpoint + deliberately invalid creds,
        # so any S3 call it makes fails loudly (InvalidAccessKeyId).
        "AWS_ACCESS_KEY_ID": "AKIACONFORMANCEREVOKED",
        "AWS_SECRET_ACCESS_KEY": "conformance-revoked-secret",
        "AGENT_RUNNER_ACCESS": "mcp",
        "MCP_URL": mcp_url,
        "RUN_TOKEN": run_token,
        "YOLOSCRIBE_MODEL": "haiku",
        "LOCAL_MODE": "true",
        "SQS_QUEUE_URL": "",
        "SQS_INDEXING_QUEUE_URL": "",
        "DDB_AGENT_LOCKS_TABLE": "",  # skip DDB entirely; not what R1 is testing
        "S3_VECTORS_BUCKET": "",  # NullSearchBackend — page-agent search goes via the MCP anyway
    }
    # These must be ABSENT (an empty AWS_PROFILE makes botocore look for a profile
    # named "" → ProfileNotFound; a stale endpoint/session token would misdirect).
    for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "S3_ENDPOINT_URL", "MINIO_ACCESS_KEY_ID", "MINIO_SECRET_ACCESS_KEY"):
        env.pop(k, None)
    proc = subprocess.run(
        [sys.executable, "-m", "agent_runner.agent_runner"],
        cwd=_AGENT_RUNNER_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    log_text = proc.stdout + proc.stderr

    # 4. Read the page back through the MCP and inspect the runner's logs.
    verify_token, _ = _mint(cfg)
    with HttpMCPClient(mcp_url, verify_token) as c:
        final, _ = c.wiki_read(_SCRATCH_PAGE)

    _S3_ERROR_MARKERS = (
        "InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied",
        "InvalidClientTokenId", "botocore.exceptions", "EndpointConnectionError",
    )
    saw_s3_error = any(m in log_text for m in _S3_ERROR_MARKERS)

    checklist = {
        "runner selected the MCP IO path": "Runner IO via MCP run token" in log_text,
        "agent.md parsed without an S3 read": "agent.md not found" not in log_text,
        "run reached completion": "Agent run complete" in log_text,
        "no S3 access error anywhere in the runner's own logs": not saw_s3_error,
        "content.md changed vs seed (write landed via MCP)": final != _SEED and final.strip() != "",
        "requested edit is present (R1-OK)": "R1-OK" in final,
    }
    conformant = all(checklist.values())
    REPORT.record(
        RResult(
            id="R1",
            name="No-bucket",
            status="PASS" if conformant else "FAIL",
            detail=(
                "runner's own S3 access revoked; full Page cycle (read → write → "
                "run-log → notify) completed through the MCP with a page-scoped run "
                "token and produced no S3 access error."
                if conformant
                else "one or more R1 conditions failed — see checklist; runner log tail:\n"
                + "\n".join(log_text.strip().splitlines()[-25:])
            ),
            checklist=checklist,
        )
    )

    # 5. Best-effort cleanup — blank the scratch page (delete needs a scope the
    #    run token intentionally lacks). Leave the site otherwise untouched.
    try:
        with HttpMCPClient(mcp_url, verify_token) as c:
            c.wiki_write(_SCRATCH_PAGE, "# R1 Conformance\n\n(cleaned)\n", "R1 conformance cleanup")
    except Exception:
        pass

    assert conformant, REPORT._results["R1"].detail
