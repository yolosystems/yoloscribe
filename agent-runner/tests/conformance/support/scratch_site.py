"""Scratch-site helpers for the Tier B (live-infra) conformance tests.

A "scratch site" is a fresh `conformance-<uuid>` prefix, isolated per test run.
Tier A tests (R2/R3a/R4/R7) build one against `LocalStorageBackend` directly —
no helpers needed there. Tier B tests (R1/R3b) need real MinIO/dynamodb-local,
reachable after `docker compose up -d minio minio-init dynamodb-local
dynamodb-init elasticmq` — see repo root Makefile.
"""
from __future__ import annotations

import os
import uuid

import boto3

MINIO_ENDPOINT = os.environ.get("CONFORMANCE_MINIO_ENDPOINT", "http://localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("CONFORMANCE_MINIO_ACCESS_KEY", "yoloscribe")
MINIO_SECRET_KEY = os.environ.get("CONFORMANCE_MINIO_SECRET_KEY", "yoloscribe")
S3_BUCKET = os.environ.get("CONFORMANCE_S3_BUCKET", os.environ.get("S3_BUCKET", "yoloscribe"))
DYNAMODB_ENDPOINT = os.environ.get("CONFORMANCE_DYNAMODB_ENDPOINT", "http://localhost:8002")
DDB_AGENT_LOCKS_TABLE = "yoloscribe-agent-locks"


def new_site() -> str:
    return f"conformance-{uuid.uuid4().hex[:10]}"


def minio_client(access_key: str = MINIO_ACCESS_KEY, secret_key: str = MINIO_SECRET_KEY):
    """A boto3 S3 client against the local MinIO instance.

    Pass bogus access_key/secret_key to get a client that MinIO will reject —
    this is how R1 simulates "direct S3 access revoked for the runtime identity."
    """
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


def dynamodb_client():
    return boto3.client(
        "dynamodb",
        endpoint_url=DYNAMODB_ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )


def put(s3, key: str, body: str) -> None:
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=body.encode("utf-8"), ContentType="text/markdown; charset=utf-8")


def get(s3, key: str) -> str | None:
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
        return obj["Body"].read().decode("utf-8")
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NoSuchBucket"):
            return None
        raise


def exists(s3, key: str) -> bool:
    return get(s3, key) is not None


def cleanup(s3, site: str) -> None:
    """Best-effort delete of every object under the scratch site prefix."""
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"{site}/"):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    for i in range(0, len(keys), 1000):
        batch = keys[i : i + 1000]
        s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": [{"Key": key} for key in batch]})
