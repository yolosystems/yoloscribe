"""R1 — No-bucket.

Contract §3 / §8: with direct S3 access revoked for the runtime identity, a
full Page + Ingest + Notification cycle still completes end-to-end (proves
all mutation goes through the MCP).

Tier B (real MinIO, needs `docker compose up`). Revocation is simulated with
invalid MinIO credentials — cheap, same observable AccessDenied outcome as a
real deny-policy identity (confirmed as the S0.1 approach; upgrade later if
Phase 1's P1.6 live cutover wants the stronger guarantee).

A single Page-agent scenario is representative here, not all three agent
types: `agent_runner.main()`'s very first storage operation — before any
agent-type dispatch happens — is `storage.read(AGENT_MD_KEY)`, a direct S3
GetObject (agent_runner.py:~993). The failure happens before agent type even
matters, so exercising Page/Ingest/Notification separately would be
redundant for what R1 tests today.

Note: `agent_runner.main()` catches every exception internally and returns
normally (exit code 0) rather than propagating — it does NOT crash the
process. So conformance here can't be "process exits non-zero"; instead this
test uses a *separate, validly-credentialed* verifier client (playing the
role of the test harness's own identity, distinct from the revoked runtime
identity) to seed the scratch site beforehand and confirm afterward that
nothing the run should have produced — run_log.md, the signal log, the
proposed/updated content — actually landed in the bucket.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from yoloscribe_io import AgentDefinition, build_agent_md

from .support.report import REPORT, RResult
from .support.scratch_site import S3_BUCKET, cleanup, exists, minio_client, new_site, put

_AGENT_RUNNER_DIR = Path(__file__).resolve().parents[2]


@pytest.mark.conformance_live
def test_r1_no_bucket():
    verifier = minio_client()  # valid credentials — the test's own identity, not the runtime's
    site = new_site()
    agent_name = "r1-page-agent"
    agent_md_key = f"{site}/.agents/{agent_name}/agent.md"
    content_key = f"{site}/content.md"
    run_log_key = f"{site}/.agents/{agent_name}/run_log.md"
    signal_log_key = f"{site}/.user/librarian/signal-log.md"

    agent_def = AgentDefinition(
        name=agent_name,
        trigger="manual",
        type="page",
        description="Rewrite the page for R1.",
    )
    put(verifier, agent_md_key, build_agent_md(agent_def))
    put(verifier, content_key, "# Original\n\nOriginal content.\n")

    try:
        env = {
            **os.environ,
            "BUCKET": S3_BUCKET,
            "AGENT_MD_KEY": agent_md_key,
            "CONTENT_KEY": content_key,
            "AGENT_PROMPT": "Rewrite this page.",
            "USER_ID": "conformance-r1-user",
            "AWS_REGION": "us-east-1",
            "S3_ENDPOINT_URL": "http://localhost:9000",
            # The revoked runtime identity: invalid credentials against real MinIO.
            "MINIO_ACCESS_KEY_ID": "invalid-revoked-key",
            "MINIO_SECRET_ACCESS_KEY": "invalid-revoked-secret",
            "LOCAL_MODE": "true",
            "SQS_QUEUE_URL": "",
            "DDB_AGENT_LOCKS_TABLE": "",  # skip DDB entirely; not what R1 is testing
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "unused-r1-never-reaches-model"),
        }
        proc = subprocess.run(
            [sys.executable, "-m", "agent_runner.agent_runner"],
            cwd=_AGENT_RUNNER_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        log_text = proc.stdout + proc.stderr
        saw_agent_execution_failed = "Agent execution failed" in log_text
        saw_access_error = any(
            code in log_text
            for code in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied", "403")
        )
        no_run_log_written = not exists(verifier, run_log_key)
        no_signal_written = not exists(verifier, signal_log_key)
        content_unchanged = verifier.get_object(Bucket=S3_BUCKET, Key=content_key)["Body"].read().decode(
            "utf-8"
        ) == "# Original\n\nOriginal content.\n"

        checklist = {
            "runner process exits (doesn't hang)": proc.returncode is not None,
            "log shows 'Agent execution failed'": saw_agent_execution_failed,
            "log shows an S3 access-denied error code": saw_access_error,
            "no run_log.md written (best-effort write also failed under revoked creds)": no_run_log_written,
            "no signal log written": no_signal_written,
            "live content.md unchanged": content_unchanged,
        }
        # R1 conformance would mean the cycle completes end-to-end anyway (MCP
        # path). Today it does the opposite — every side effect is missing —
        # which is exactly why this is a FAIL, not a PASS.
        conformant = False  # today's runner has no MCP path at all; can't pass R1 by construction
        REPORT.record(
            RResult(
                id="R1",
                name="No-bucket",
                status="PASS" if conformant else "FAIL",
                detail=(
                    "runner performs a direct S3 GetObject (storage.read(AGENT_MD_KEY)) as its "
                    "very first operation — before any MCP-equivalent call — so revoking the "
                    "runtime identity's S3 access fails the run immediately. main() swallows the "
                    "exception and exits 0, but produces none of its expected side effects."
                ),
                checklist=checklist,
            )
        )
        assert saw_agent_execution_failed and saw_access_error, log_text
    finally:
        cleanup(verifier, site)
