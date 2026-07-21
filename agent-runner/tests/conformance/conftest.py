"""Shared fixtures and session hooks for the conformance harness (R1-R7).

Importing `agent_runner.polling_worker` (needed by R3b to exercise the real
`_acquire_page_lock`) reads `SQS_QUEUE_URL` from the environment at import
time with no default. Seed a placeholder before collection so Tier A tests
can still import it (and thus be collected) even when nothing SQS-shaped is
running — R3b never actually sends a message, it only calls the lock
function directly.
"""
from __future__ import annotations

import os

os.environ.setdefault("SQS_QUEUE_URL", "http://localhost:9324/000000000000/yoloscribe-runner-conformance")

from pathlib import Path  # noqa: E402

import boto3  # noqa: E402
import pytest  # noqa: E402

from .support.report import REPORT, git_sha  # noqa: E402
from .support.scratch_site import (  # noqa: E402
    DDB_AGENT_LOCKS_TABLE,
    DYNAMODB_ENDPOINT,
    MINIO_ACCESS_KEY,
    MINIO_ENDPOINT,
    MINIO_SECRET_KEY,
    S3_BUCKET,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BASELINE_PATH = Path(__file__).resolve().parent / "BASELINE.md"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "conformance: fast, in-process conformance checks (default `make conformance` target)"
    )
    config.addinivalue_line(
        "markers",
        "conformance_live: needs `docker compose up -d minio minio-init dynamodb-local "
        "dynamodb-init elasticmq` (see repo root Makefile: `make conformance-live`)",
    )


def _live_infra_available() -> bool:
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=MINIO_ENDPOINT,
            region_name="us-east-1",
            aws_access_key_id=MINIO_ACCESS_KEY,
            aws_secret_access_key=MINIO_SECRET_KEY,
        )
        s3.head_bucket(Bucket=S3_BUCKET)
        ddb = boto3.client(
            "dynamodb",
            endpoint_url=DYNAMODB_ENDPOINT,
            region_name="us-east-1",
            aws_access_key_id="local",
            aws_secret_access_key="local",
        )
        ddb.describe_table(TableName=DDB_AGENT_LOCKS_TABLE)
        return True
    except Exception:
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if _live_infra_available():
        return
    skip_live = pytest.mark.skip(
        reason=(
            "conformance_live requires `docker compose up -d minio minio-init "
            "dynamodb-local dynamodb-init elasticmq` — run `make conformance-live` instead"
        )
    )
    for item in items:
        if "conformance_live" in item.keywords:
            item.add_marker(skip_live)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    print(REPORT.render_table())
    if REPORT.has_full_session():
        _BASELINE_PATH.write_text(REPORT.render_baseline_md(git_sha(_REPO_ROOT)))
        print(f"\nBaseline written to {_BASELINE_PATH}")
    else:
        print(
            "\n(BASELINE.md not regenerated — run `make conformance-live` for the "
            "full R1-R7 session needed to update it.)"
        )
