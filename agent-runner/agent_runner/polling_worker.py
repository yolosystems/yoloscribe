"""Polling worker — long-running SQS poller that dispatches K8s Jobs / CronJobs.

Environment variables:
    SQS_QUEUE_URL           SQS queue to poll
    AWS_REGION              AWS region
    AGENT_RUNNER_IMAGE      Docker image for the agent-runner Job container
    ANTHROPIC_SECRET_NAME   K8s Secret name holding the Anthropic API key
    K8S_NAMESPACE           Kubernetes namespace for Jobs/CronJobs
    AWS_PROFILE             (optional) named AWS profile for local development
    LOCAL_RUNNER            Set to "true" to run agents in-process instead of
                            dispatching K8s Jobs (for local development/testing)
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
from botocore.exceptions import ClientError

from yoloscribe_io import AgentDefinitionError, NotificationsMarkdownFile, S3StorageBackend, SecretsManagerStore, Webhooks, parse_agent_md

from .log_setup import configure_logging

configure_logging()
log = logging.getLogger(__name__)

SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]
SQS_INDEXING_QUEUE_URL = os.environ.get("SQS_INDEXING_QUEUE_URL", "")
DDB_AGENT_LOCKS_TABLE = os.environ.get("DDB_AGENT_LOCKS_TABLE", "yoloscribe-agent-locks")
DYNAMODB_ENDPOINT_URL = os.environ.get("DYNAMODB_ENDPOINT_URL", "")
# Seconds to hide a locked-page message before it becomes visible again.
SQS_LOCK_REQUEUE_DELAY = int(os.environ.get("SQS_LOCK_REQUEUE_DELAY", "30"))
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_RUNNER_IMAGE = os.environ.get("AGENT_RUNNER_IMAGE", "ghcr.io/nate-yolodev/yoloscribe-agent-runner:latest")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
YOLOSCRIBE_MODEL = os.environ.get("YOLOSCRIBE_MODEL", "")
YOLOSCRIBE_MODEL_BASE_URL = os.environ.get("YOLOSCRIBE_MODEL_BASE_URL", "")
OTEL_EXPORTER_OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
OTEL_EXPORTER_OTLP_HEADERS = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
PHOENIX_API_ENDPOINT = os.environ.get("PHOENIX_API_ENDPOINT", "")
K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "yoloscribe")
AWS_PROFILE = os.environ.get("AWS_PROFILE", "")
LOCAL_RUNNER = os.environ.get("LOCAL_RUNNER", "").lower() in ("1", "true", "yes")
SQS_ENDPOINT_URL = os.environ.get("SQS_ENDPOINT_URL", "")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")

# SM client — initialised in main(), None in LOCAL_RUNNER mode.
_sm_client = None


def _load_user_webhooks(user_id: str) -> str:
    """Return YOLOSCRIBE_WEBHOOKS value (JSON list of URL strings) for a user.

    Reads from SM at yoloscribe/{user_id}/webhooks. Returns '[]' when the path
    doesn't exist, SM is unavailable, or LOCAL_RUNNER is set — keeping the
    notification-mcp tool functional but no-op in those cases.
    """
    if _sm_client is None:
        return "[]"
    try:
        store = SecretsManagerStore(_sm_client)
        entries = Webhooks(user_id, store).list()
        urls = [e.url for e in entries]
        log.info("Loaded %d webhook(s) for user %s", len(urls), user_id)
        return json.dumps(urls)
    except Exception as exc:
        log.warning("Failed to load webhooks for user %s: %s", user_id, exc)
        return "[]"


def _safe_k8s_name(*parts: str, max_len: int = 63) -> str:
    """Build a DNS-label-safe K8s name from parts, truncated to max_len chars.

    Jobs allow up to 63 characters. CronJobs must be <= 52 characters because
    Kubernetes appends an 11-character suffix when creating triggered Jobs.
    """
    joined = "-".join(parts).lower()
    safe = re.sub(r"[^a-z0-9-]", "-", joined)
    safe = re.sub(r"-+", "-", safe).strip("-")
    return safe[:max_len].strip("-")


# ── Local runner ───────────────────────────────────────────────────────────────

def _run_local(payload: dict) -> None:
    """Run the agent runner in a subprocess (local dev mode, no K8s)."""
    user_id = payload.get("user_id", "default")
    env = os.environ.copy()
    env.update(
        {
            "BUCKET": payload["bucket"],
            "AGENT_MD_KEY": payload["agent_md_key"],
            "CONTENT_KEY": payload["content_key"],
            "AGENT_PROMPT": payload["prompt"],
            "USER_ID": user_id,
            "AWS_REGION": AWS_REGION,
            "YOLOSCRIBE_WEBHOOKS": _load_user_webhooks(user_id),
            "DDB_AGENT_LOCKS_TABLE": DDB_AGENT_LOCKS_TABLE,
        }
    )
    if SQS_INDEXING_QUEUE_URL:
        env["SQS_INDEXING_QUEUE_URL"] = SQS_INDEXING_QUEUE_URL
    if SQS_QUEUE_URL:
        env["SQS_QUEUE_URL"] = SQS_QUEUE_URL
    if AWS_PROFILE:
        env["AWS_PROFILE"] = AWS_PROFILE
    if PHOENIX_API_ENDPOINT:
        env["PHOENIX_API_ENDPOINT"] = PHOENIX_API_ENDPOINT

    log.info(
        "LOCAL_RUNNER: running agent-runner for %s / %s",
        payload.get("user_id"),
        payload["agent_md_key"],
    )
    subprocess.run(
        [sys.executable, "-m", "agent_runner.agent_runner"],
        env=env,
        check=True,
    )


# ── K8s runner ────────────────────────────────────────────────────────────────

def _build_container(payload: dict):  # type: ignore[return]
    from kubernetes import client as k8s_client  # noqa: PLC0415

    user_id = payload.get("user_id", "default")
    env_vars = [
        k8s_client.V1EnvVar(name="BUCKET", value=payload["bucket"]),
        k8s_client.V1EnvVar(name="AGENT_MD_KEY", value=payload["agent_md_key"]),
        k8s_client.V1EnvVar(name="CONTENT_KEY", value=payload["content_key"]),
        k8s_client.V1EnvVar(name="AGENT_PROMPT", value=payload["prompt"]),
        k8s_client.V1EnvVar(name="USER_ID", value=user_id),
        k8s_client.V1EnvVar(name="AWS_REGION", value=AWS_REGION),
        k8s_client.V1EnvVar(name="ANTHROPIC_API_KEY", value=ANTHROPIC_API_KEY),
        k8s_client.V1EnvVar(name="SQS_INDEXING_QUEUE_URL", value=SQS_INDEXING_QUEUE_URL),
        k8s_client.V1EnvVar(name="SQS_QUEUE_URL", value=SQS_QUEUE_URL),
        k8s_client.V1EnvVar(name="YOLOSCRIBE_WEBHOOKS", value=_load_user_webhooks(user_id)),
        k8s_client.V1EnvVar(name="DDB_AGENT_LOCKS_TABLE", value=DDB_AGENT_LOCKS_TABLE),
    ]
    if YOLOSCRIBE_MODEL:
        env_vars.append(k8s_client.V1EnvVar(name="YOLOSCRIBE_MODEL", value=YOLOSCRIBE_MODEL))
    if YOLOSCRIBE_MODEL_BASE_URL:
        env_vars.append(k8s_client.V1EnvVar(name="YOLOSCRIBE_MODEL_BASE_URL", value=YOLOSCRIBE_MODEL_BASE_URL))
    if OTEL_EXPORTER_OTLP_ENDPOINT:
        env_vars.append(k8s_client.V1EnvVar(name="OTEL_EXPORTER_OTLP_ENDPOINT", value=OTEL_EXPORTER_OTLP_ENDPOINT))
    if OTEL_EXPORTER_OTLP_HEADERS:
        env_vars.append(k8s_client.V1EnvVar(name="OTEL_EXPORTER_OTLP_HEADERS", value=OTEL_EXPORTER_OTLP_HEADERS))
    if PHOENIX_API_ENDPOINT:
        env_vars.append(k8s_client.V1EnvVar(name="PHOENIX_API_ENDPOINT", value=PHOENIX_API_ENDPOINT))
    return k8s_client.V1Container(
        name="agent-runner",
        image=AGENT_RUNNER_IMAGE,
        command=["uv", "run", "agent-runner"],
        env=env_vars,
    )


def _build_enqueuer_container(payload: dict):  # type: ignore[return]
    """Build a minimal container that runs agent-enqueuer (SQS send only)."""
    from kubernetes import client as k8s_client  # noqa: PLC0415

    user_id = payload.get("user_id", "default")
    return k8s_client.V1Container(
        name="agent-enqueuer",
        image=AGENT_RUNNER_IMAGE,
        command=["uv", "run", "agent-enqueuer"],
        env=[
            k8s_client.V1EnvVar(name="BUCKET", value=payload["bucket"]),
            k8s_client.V1EnvVar(name="AGENT_MD_KEY", value=payload["agent_md_key"]),
            k8s_client.V1EnvVar(name="CONTENT_KEY", value=payload["content_key"]),
            k8s_client.V1EnvVar(name="AGENT_PROMPT", value=payload["prompt"]),
            k8s_client.V1EnvVar(name="USER_ID", value=user_id),
            k8s_client.V1EnvVar(name="AWS_REGION", value=AWS_REGION),
            k8s_client.V1EnvVar(name="SQS_QUEUE_URL", value=SQS_QUEUE_URL),
        ],
    )


def _pod_spec(container, user_id: str, image_pull_secrets=None):  # type: ignore[return]
    from kubernetes import client as k8s_client  # noqa: PLC0415

    return k8s_client.V1PodSpec(
        service_account_name=f"user-{user_id}",
        restart_policy="Never",
        containers=[container],
        image_pull_secrets=image_pull_secrets,
    )


def _create_job(batch_v1, name: str, pod_spec) -> None:  # type: ignore[return]
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
    log.info("Created Job %s", name)


def _upsert_cronjob(batch_v1, name: str, pod_spec, schedule: str, timezone: str) -> None:
    from kubernetes import client as k8s_client  # noqa: PLC0415
    from kubernetes.client.rest import ApiException  # noqa: PLC0415

    cron_spec = k8s_client.V1CronJobSpec(
        schedule=schedule,
        concurrency_policy="Forbid",
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


def _write_notification_to_s3(s3, bucket: str, site: str, event_type: str, payload: dict) -> None:
    """Append a canonical notification entry to {site}/.user/notifications.md."""
    storage = S3StorageBackend(bucket, s3)
    try:
        NotificationsMarkdownFile(site, storage).notify(event_type, payload)
    except Exception as exc:
        log.error("Failed to write notification for site %s: %s", site, exc)


def _acquire_page_lock(ddb, user_id: str, content_key: str) -> bool:
    """Try to acquire a DDB page lock for the given content key.

    Returns True if the lock was acquired (or DDB is unavailable — fail open).
    Returns False if the page is already locked by another running job.
    """
    if ddb is None or not DDB_AGENT_LOCKS_TABLE:
        return True
    now = int(time.time())
    try:
        ddb.put_item(
            TableName=DDB_AGENT_LOCKS_TABLE,
            Item={
                "user_id": {"S": user_id},
                "page_path": {"S": content_key},
                "expires_at": {"N": str(now + 3600)},
            },
            ConditionExpression="attribute_not_exists(user_id) OR expires_at < :now",
            ExpressionAttributeValues={":now": {"N": str(now)}},
        )
        log.info("Acquired page lock: %s / %s", user_id, content_key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            log.info("Page locked by another job — requeueing: %s", content_key)
            return False
        log.warning("DDB lock error for %s — proceeding without lock", content_key, exc_info=True)
        return True  # fail open: don't block if DDB is misconfigured
    except Exception:
        log.warning("DDB lock error for %s — proceeding without lock", content_key, exc_info=True)
        return True


def _process_message_k8s(batch_v1, s3, ddb, payload: dict, image_pull_secrets=None) -> bool:
    """Dispatch the agent job. Returns True if the message should be requeued (lock conflict)."""
    user_id = payload.get("user_id", "default")
    bucket = payload["bucket"]
    agent_md_key = payload["agent_md_key"]

    # Derive site and agent name from keys for K8s name construction
    # agent_md_key: {site}/[{page}/].agents/{agent_name}/agent.md
    parts = agent_md_key.split("/")
    site = parts[0] if parts else "unknown"
    agent_name = parts[-2] if len(parts) >= 2 else "unknown"

    raw_agent_md = S3StorageBackend(bucket, s3).read(agent_md_key) or ""
    try:
        agent_def = parse_agent_md(raw_agent_md)
    except AgentDefinitionError as exc:
        log.error("Invalid agent.md at %s: %s", agent_md_key, exc)
        _write_notification_to_s3(
            s3, bucket, site, "agent_failure",
            {"agent": agent_md_key, "reason": f"Invalid agent definition: {exc}"},
        )
        return False

    container = _build_container(payload)
    pod_spec = _pod_spec(container, user_id, image_pull_secrets=image_pull_secrets)

    if agent_def.trigger == "schedule":
        # CronJob: pod just enqueues an SQS message; the polling worker's lock
        # check and serialization logic applies when that message is later dequeued.
        enqueuer_container = _build_enqueuer_container(payload)
        enqueuer_pod_spec = _pod_spec(enqueuer_container, user_id, image_pull_secrets=image_pull_secrets)
        cron_name = _safe_k8s_name("agentrunner", site, agent_name, user_id, max_len=52)
        _upsert_cronjob(
            batch_v1,
            name=cron_name,
            pod_spec=enqueuer_pod_spec,
            schedule=agent_def.schedule,
            timezone=agent_def.timezone,
        )
    else:
        content_key = payload.get("content_key", "")
        if not _acquire_page_lock(ddb, user_id, content_key):
            return True  # requeue — another job is running for this page
        ts = str(int(time.time()))
        prefix = _safe_k8s_name("agentrunner", site, agent_name, user_id)
        max_prefix = 63 - 1 - len(ts)  # reserve room for "-{ts}"
        job_name = f"{prefix[:max_prefix].rstrip('-')}-{ts}"
        _create_job(batch_v1, name=job_name, pod_spec=pod_spec)
    return False


def main() -> None:
    global _sm_client
    _session = boto3.Session(profile_name=AWS_PROFILE or None)
    _sqs_kwargs: dict = {"region_name": AWS_REGION}
    if SQS_ENDPOINT_URL:
        _sqs_kwargs["endpoint_url"] = SQS_ENDPOINT_URL
        _sqs_kwargs["aws_access_key_id"] = os.environ.get("ELASTICMQ_ACCESS_KEY_ID", "test")
        _sqs_kwargs["aws_secret_access_key"] = os.environ.get("ELASTICMQ_SECRET_ACCESS_KEY", "test")
    _s3_kwargs: dict = {"region_name": AWS_REGION}
    if S3_ENDPOINT_URL:
        _s3_kwargs["endpoint_url"] = S3_ENDPOINT_URL
        _s3_kwargs["aws_access_key_id"] = os.environ.get("MINIO_ACCESS_KEY_ID", "yoloscribe")
        _s3_kwargs["aws_secret_access_key"] = os.environ.get("MINIO_SECRET_ACCESS_KEY", "yoloscribe")
    sqs = _session.client("sqs", **_sqs_kwargs)
    s3 = _session.client("s3", **_s3_kwargs)

    image_pull_secrets: list = []
    if LOCAL_RUNNER:
        log.info("LOCAL_RUNNER mode — agents will run as subprocesses (no K8s)")
        batch_v1 = None
        ddb = None
        _sm_client = None
    else:
        from kubernetes import client as k8s_client  # type: ignore[import-untyped]  # noqa: PLC0415
        from kubernetes import config as k8s_config  # type: ignore[import-untyped]  # noqa: PLC0415

        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        batch_v1 = k8s_client.BatchV1Api()
        _sm_client = _session.client("secretsmanager", region_name=AWS_REGION)

        _ddb_kwargs: dict = {"region_name": AWS_REGION}
        if DYNAMODB_ENDPOINT_URL:
            _ddb_kwargs["endpoint_url"] = DYNAMODB_ENDPOINT_URL
        ddb = _session.client("dynamodb", **_ddb_kwargs)

        # Inherit imagePullSecrets from this pod so spawned jobs can pull the same image.
        try:
            core_v1 = k8s_client.CoreV1Api()
            pod_name = os.environ.get("HOSTNAME", "")
            own_pod = core_v1.read_namespaced_pod(name=pod_name, namespace=K8S_NAMESPACE)
            image_pull_secrets = own_pod.spec.image_pull_secrets or []
        except Exception:
            log.warning("Could not read own pod spec to inherit imagePullSecrets", exc_info=True)
            image_pull_secrets = []

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
                requeued = False
                try:
                    payload = json.loads(msg["Body"])
                    if LOCAL_RUNNER:
                        _run_local(payload)
                    else:
                        requeued = _process_message_k8s(batch_v1, s3, ddb, payload, image_pull_secrets=image_pull_secrets)
                except Exception:
                    log.exception("Failed to process message %s", msg.get("MessageId"))
                if requeued:
                    # Page is locked — return the message to the queue after a short delay.
                    try:
                        sqs.change_message_visibility(
                            QueueUrl=SQS_QUEUE_URL,
                            ReceiptHandle=receipt,
                            VisibilityTimeout=SQS_LOCK_REQUEUE_DELAY,
                        )
                    except Exception:
                        log.warning("Failed to requeue locked message %s", msg.get("MessageId"), exc_info=True)
                else:
                    # Delete the message — failed messages do not loop; use a DLQ for replay.
                    sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt)
        except Exception:
            log.exception("SQS receive error — retrying in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
