"""Polling worker — long-running SQS poller that dispatches K8s Jobs / CronJobs.

Environment variables:
    SQS_QUEUE_URL           SQS queue to poll
    AWS_REGION              AWS region
    AGENT_RUNNER_IMAGE      Docker image for the agent-runner Job container
    ANTHROPIC_SECRET_NAME   K8s Secret name holding the Anthropic API key
    K8S_NAMESPACE           Kubernetes namespace for Jobs/CronJobs
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

import boto3
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

from .parse import parse_agent_md

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_RUNNER_IMAGE = os.environ.get("AGENT_RUNNER_IMAGE", "ghcr.io/nate-yolodev/agentscribe-agent-runner:latest")
ANTHROPIC_SECRET_NAME = os.environ.get("ANTHROPIC_SECRET_NAME", "agentscribe-secrets")
K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "agentscribe")


def _safe_k8s_name(*parts: str) -> str:
    """Build a DNS-label-safe K8s name from parts, max 63 chars."""
    joined = "-".join(parts).lower()
    safe = re.sub(r"[^a-z0-9-]", "-", joined)
    safe = re.sub(r"-+", "-", safe).strip("-")
    return safe[:63]


def _build_container(payload: dict) -> k8s_client.V1Container:
    env_vars = [
        k8s_client.V1EnvVar(name="BUCKET", value=payload["bucket"]),
        k8s_client.V1EnvVar(name="AGENT_MD_KEY", value=payload["agent_md_key"]),
        k8s_client.V1EnvVar(name="CONTENT_KEY", value=payload["content_key"]),
        k8s_client.V1EnvVar(name="PROMPT", value=payload["prompt"]),
        k8s_client.V1EnvVar(name="USER_ID", value=payload.get("user_id", "default")),
        k8s_client.V1EnvVar(name="AWS_REGION", value=AWS_REGION),
        k8s_client.V1EnvVar(
            name="ANTHROPIC_API_KEY",
            value_from=k8s_client.V1EnvVarSource(
                secret_key_ref=k8s_client.V1SecretKeySelector(
                    name=ANTHROPIC_SECRET_NAME,
                    key="anthropic-api-key",
                    optional=True,
                )
            ),
        ),
    ]
    return k8s_client.V1Container(
        name="agent-runner",
        image=AGENT_RUNNER_IMAGE,
        command=["uv", "run", "agent-runner"],
        env=env_vars,
    )


def _pod_spec(container: k8s_client.V1Container, user_id: str) -> k8s_client.V1PodSpec:
    return k8s_client.V1PodSpec(
        service_account_name=f"user-{user_id}",
        restart_policy="Never",
        containers=[container],
    )


def _create_job(batch_v1: k8s_client.BatchV1Api, name: str, pod_spec: k8s_client.V1PodSpec) -> None:
    job = k8s_client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=K8S_NAMESPACE),
        spec=k8s_client.V1JobSpec(
            template=k8s_client.V1PodTemplateSpec(
                spec=pod_spec,
            ),
            backoff_limit=0,
            ttl_seconds_after_finished=3600,
        ),
    )
    batch_v1.create_namespaced_job(namespace=K8S_NAMESPACE, body=job)
    log.info("Created Job %s", name)


def _upsert_cronjob(
    batch_v1: k8s_client.BatchV1Api,
    name: str,
    pod_spec: k8s_client.V1PodSpec,
    schedule: str,
    timezone: str,
) -> None:
    cron_spec = k8s_client.V1CronJobSpec(
        schedule=schedule,
        job_template=k8s_client.V1JobTemplateSpec(
            spec=k8s_client.V1JobSpec(
                template=k8s_client.V1PodTemplateSpec(spec=pod_spec),
                backoff_limit=0,
            )
        ),
        successful_jobs_history_limit=3,
        failed_jobs_history_limit=1,
    )
    if timezone:
        cron_spec.time_zone = timezone

    cron_job = k8s_client.V1CronJob(
        api_version="batch/v1",
        kind="CronJob",
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=K8S_NAMESPACE),
        spec=cron_spec,
    )
    try:
        batch_v1.replace_namespaced_cron_job(name=name, namespace=K8S_NAMESPACE, body=cron_job)
        log.info("Updated CronJob %s", name)
    except ApiException as exc:
        if exc.status == 404:
            batch_v1.create_namespaced_cron_job(namespace=K8S_NAMESPACE, body=cron_job)
            log.info("Created CronJob %s", name)
        else:
            raise


def _process_message(batch_v1: k8s_client.BatchV1Api, s3, payload: dict) -> None:
    user_id = payload.get("user_id", "default")
    bucket = payload["bucket"]
    agent_md_key = payload["agent_md_key"]

    # Parse agent.md to get name and schedule info
    obj = s3.get_object(Bucket=bucket, Key=agent_md_key)
    agent_md_text = obj["Body"].read().decode("utf-8")
    agent_def = parse_agent_md(agent_md_text)

    # Derive site and agent name from keys for K8s name construction
    # agent_md_key: {site}/[{page}/].agents/{agent_name}/agent.md
    parts = agent_md_key.split("/")
    site = parts[0] if parts else "unknown"
    agent_name = parts[-2] if len(parts) >= 2 else "unknown"

    container = _build_container(payload)
    pod_spec = _pod_spec(container, user_id)

    if agent_def.schedule:
        # Stable name for idempotent CronJob upserts
        cron_name = _safe_k8s_name("agentrunner", site, agent_name, user_id)
        _upsert_cronjob(
            batch_v1,
            name=cron_name,
            pod_spec=pod_spec,
            schedule=agent_def.schedule,
            timezone=agent_def.timezone,
        )
    else:
        ts = str(int(time.time()))
        job_name = _safe_k8s_name("agentrunner", site, agent_name, user_id, ts)
        _create_job(batch_v1, name=job_name, pod_spec=pod_spec)


def main() -> None:
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()

    batch_v1 = k8s_client.BatchV1Api()
    sqs = boto3.client("sqs", region_name=AWS_REGION)
    s3 = boto3.client("s3", region_name=AWS_REGION)

    log.info("Polling SQS queue: %s", SQS_QUEUE_URL)
    while True:
        try:
            resp = sqs.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                WaitTimeSeconds=20,
                MaxNumberOfMessages=10,
            )
            for msg in resp.get("Messages", []):
                receipt = msg["ReceiptHandle"]
                try:
                    payload = json.loads(msg["Body"])
                    _process_message(batch_v1, s3, payload)
                    sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt)
                except Exception:
                    log.exception("Failed to process message %s", msg.get("MessageId"))
        except Exception:
            log.exception("SQS receive error — retrying in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
