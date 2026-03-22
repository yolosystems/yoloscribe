"""Indexer polling worker — SQS poller that dispatches K8s Jobs for content indexing.

Environment variables:
    SQS_INDEXING_QUEUE_URL      SQS queue to poll for indexing jobs
    AWS_REGION                  AWS region
    INDEXER_IMAGE               Docker image for the indexer Job container
    K8S_NAMESPACE               Kubernetes namespace for Jobs
    S3_VECTORS_BUCKET           S3 Vectors bucket name
    S3_VECTORS_INDEX_NAME       S3 Vectors index name (default: yoloscribe)
    BEDROCK_EMBEDDING_MODEL     Bedrock embedding model ID
    AWS_PROFILE                 (optional) named AWS profile for local development
    LOCAL_RUNNER                Set to "true" to run index-runner in subprocess
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time

import boto3

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SQS_INDEXING_QUEUE_URL = os.environ["SQS_INDEXING_QUEUE_URL"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
INDEXER_IMAGE = os.environ.get("INDEXER_IMAGE", "ghcr.io/nate-yolodev/yoloscribe-indexer:latest")
K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "yoloscribe")
INDEXER_SERVICE_ACCOUNT = os.environ.get("INDEXER_SERVICE_ACCOUNT", "yoloscribe-indexer")
S3_VECTORS_BUCKET = os.environ.get("S3_VECTORS_BUCKET", "")
S3_VECTORS_INDEX_NAME = os.environ.get("S3_VECTORS_INDEX_NAME", "yoloscribe")
BEDROCK_EMBEDDING_MODEL = os.environ.get("BEDROCK_EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")
AWS_PROFILE = os.environ.get("AWS_PROFILE", "")
LOCAL_RUNNER = os.environ.get("LOCAL_RUNNER", "").lower() in ("1", "true", "yes")


def _safe_k8s_name(*parts: str) -> str:
    """Build a DNS-label-safe K8s name from parts, max 63 chars."""
    joined = "-".join(parts).lower()
    safe = re.sub(r"[^a-z0-9-]", "-", joined)
    safe = re.sub(r"-+", "-", safe).strip("-")
    return safe[:63].strip("-")


# ── Local runner ───────────────────────────────────────────────────────────────

def _run_local(payload: dict) -> None:
    """Run index-runner in a subprocess (local dev mode, no K8s)."""
    env = os.environ.copy()
    env.update(
        {
            "BUCKET": payload["bucket"],
            "CONTENT_KEY": payload["content_key"],
            "USER_ID": payload.get("user_id", "unknown"),
            "S3_VECTORS_BUCKET": S3_VECTORS_BUCKET,
            "S3_VECTORS_INDEX_NAME": S3_VECTORS_INDEX_NAME,
            "AWS_REGION": AWS_REGION,
            "BEDROCK_EMBEDDING_MODEL": BEDROCK_EMBEDDING_MODEL,
        }
    )
    if AWS_PROFILE:
        env["AWS_PROFILE"] = AWS_PROFILE

    log.info("LOCAL_RUNNER: indexing %s", payload["content_key"])
    subprocess.run(
        [sys.executable, "-m", "indexer.index_runner"],
        env=env,
        check=True,
    )


# ── K8s runner ────────────────────────────────────────────────────────────────

def _build_container(payload: dict):  # type: ignore[return]
    from kubernetes import client as k8s_client  # noqa: PLC0415

    env_vars = [
        k8s_client.V1EnvVar(name="BUCKET", value=payload["bucket"]),
        k8s_client.V1EnvVar(name="CONTENT_KEY", value=payload["content_key"]),
        k8s_client.V1EnvVar(name="USER_ID", value=payload.get("user_id", "unknown")),
        k8s_client.V1EnvVar(name="S3_VECTORS_BUCKET", value=S3_VECTORS_BUCKET),
        k8s_client.V1EnvVar(name="S3_VECTORS_INDEX_NAME", value=S3_VECTORS_INDEX_NAME),
        k8s_client.V1EnvVar(name="AWS_REGION", value=AWS_REGION),
        k8s_client.V1EnvVar(name="BEDROCK_EMBEDDING_MODEL", value=BEDROCK_EMBEDDING_MODEL),
    ]
    return k8s_client.V1Container(
        name="index-runner",
        image=INDEXER_IMAGE,
        command=["uv", "run", "index-runner"],
        env=env_vars,
    )


def _pod_spec(container, image_pull_secrets=None):  # type: ignore[return]
    from kubernetes import client as k8s_client  # noqa: PLC0415

    return k8s_client.V1PodSpec(
        service_account_name=INDEXER_SERVICE_ACCOUNT,
        restart_policy="Never",
        containers=[container],
        image_pull_secrets=image_pull_secrets,
    )


def _create_job(batch_v1, name: str, pod_spec) -> None:
    from kubernetes import client as k8s_client  # noqa: PLC0415

    job = k8s_client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=K8S_NAMESPACE),
        spec=k8s_client.V1JobSpec(
            template=k8s_client.V1PodTemplateSpec(spec=pod_spec),
            backoff_limit=0,
            ttl_seconds_after_finished=3600,
        ),
    )
    batch_v1.create_namespaced_job(namespace=K8S_NAMESPACE, body=job)
    log.info("Created indexer Job %s", name)


def _process_message_k8s(batch_v1, payload: dict, image_pull_secrets=None) -> None:
    content_key = payload["content_key"]
    # Derive site from content_key: "{site}/..." or "{site}/{page}/content.md"
    site = content_key.split("/")[0] if "/" in content_key else content_key

    container = _build_container(payload)
    pod_spec = _pod_spec(container, image_pull_secrets=image_pull_secrets)

    ts = str(int(time.time()))
    prefix = _safe_k8s_name("yoloscribe-indexer", site)
    max_prefix = 63 - 1 - len(ts)
    job_name = f"{prefix[:max_prefix].rstrip('-')}-{ts}"
    _create_job(batch_v1, name=job_name, pod_spec=pod_spec)


def main() -> None:
    _session = boto3.Session(profile_name=AWS_PROFILE or None)
    sqs = _session.client("sqs", region_name=AWS_REGION)

    image_pull_secrets: list = []
    if LOCAL_RUNNER:
        log.info("LOCAL_RUNNER mode — index-runner will run as subprocess (no K8s)")
        batch_v1 = None
    else:
        from kubernetes import client as k8s_client  # type: ignore[import-untyped]  # noqa: PLC0415
        from kubernetes import config as k8s_config  # type: ignore[import-untyped]  # noqa: PLC0415

        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        batch_v1 = k8s_client.BatchV1Api()

        try:
            core_v1 = k8s_client.CoreV1Api()
            pod_name = os.environ.get("HOSTNAME", "")
            own_pod = core_v1.read_namespaced_pod(name=pod_name, namespace=K8S_NAMESPACE)
            image_pull_secrets = own_pod.spec.image_pull_secrets or []
        except Exception:
            log.warning("Could not read own pod spec to inherit imagePullSecrets", exc_info=True)
            image_pull_secrets = []

    log.info("Polling SQS indexing queue: %s", SQS_INDEXING_QUEUE_URL)
    while True:
        try:
            resp = sqs.receive_message(
                QueueUrl=SQS_INDEXING_QUEUE_URL,
                WaitTimeSeconds=20,
                MaxNumberOfMessages=10,
            )
            for msg in resp.get("Messages", []):
                receipt = msg["ReceiptHandle"]
                try:
                    payload = json.loads(msg["Body"])
                    log.info("Indexing: %s", payload.get("content_key", "<unknown>"))
                    if LOCAL_RUNNER:
                        _run_local(payload)
                    else:
                        _process_message_k8s(batch_v1, payload, image_pull_secrets=image_pull_secrets)
                    sqs.delete_message(QueueUrl=SQS_INDEXING_QUEUE_URL, ReceiptHandle=receipt)
                except Exception:
                    log.exception("Failed to process indexing message %s", msg.get("MessageId"))
        except Exception:
            log.exception("SQS receive error — retrying in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
