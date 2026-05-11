"""K8s CronJob lifecycle management for scheduled agents.

Provides two best-effort operations called from both the REST API and the MCP server:
  - enqueue_schedule_bootstrap: sends an SQS message so the polling worker creates
    or updates the K8s CronJob for a newly saved scheduled agent.
  - delete_agent_cronjob: deletes the K8s CronJob when a scheduled agent is removed.

Both functions are no-ops in LOCAL_MODE and never raise — failures are logged only.

CronJob naming must stay in sync with agent-runner/agent_runner/polling_worker.py.
"""

import json
import logging
import re

log = logging.getLogger(__name__)

_SCHEDULED_PROMPT = "Run your scheduled update for this page."


def _safe_k8s_name(*parts: str, max_len: int = 63) -> str:
    joined = "-".join(parts).lower()
    safe = re.sub(r"[^a-z0-9-]", "-", joined)
    safe = re.sub(r"-+", "-", safe).strip("-")
    return safe[:max_len].strip("-")


def cronjob_name(site: str, agent_name: str, user_id: str) -> str:
    """Deterministic K8s CronJob name for a scheduled agent (max 52 chars)."""
    return _safe_k8s_name("agentrunner", site, agent_name, user_id, max_len=52)


def _page_content_key(agent_md_key: str) -> str:
    """Derive the page content.md S3 key from a full agent.md S3 key.

    agent_md_key: "{site}[/{page}]/.agents/{name}/agent.md"
    """
    agents_idx = agent_md_key.find("/.agents/")
    if agents_idx == -1:
        # Root-level agent: "{site}/.agents/{name}/agent.md"
        site = agent_md_key.split("/")[0]
        return f"{site}/content.md"
    page_prefix = agent_md_key[:agents_idx]  # "{site}" or "{site}/{page}"
    return f"{page_prefix}/content.md"


def enqueue_schedule_bootstrap(agent_md_key: str, user_id: str) -> None:
    """Send an SQS message that causes the polling worker to create/update the CronJob.

    The polling worker already knows how to build a CronJob from this payload format —
    this is the same message shape used when manually running a scheduled agent.
    """
    from config import LOCAL_MODE, S3_BUCKET, SQS_QUEUE_URL, sqs  # noqa: PLC0415

    if LOCAL_MODE:
        return
    if sqs is None or not SQS_QUEUE_URL:
        log.debug("SQS not configured; skipping schedule bootstrap for %s", agent_md_key)
        return

    payload = {
        "bucket": S3_BUCKET,
        "agent_md_key": agent_md_key,
        "content_key": _page_content_key(agent_md_key),
        "prompt": _SCHEDULED_PROMPT,
        "user_id": user_id,
    }
    try:
        sqs.send_message(QueueUrl=SQS_QUEUE_URL, MessageBody=json.dumps(payload))
        log.info("Enqueued schedule bootstrap for agent %s (user %s)", agent_md_key, user_id)
    except Exception as exc:
        log.error("Failed to enqueue schedule bootstrap for %s: %s", agent_md_key, exc)


def delete_agent_cronjob(site: str, agent_name: str, user_id: str) -> None:
    """Delete the K8s CronJob for a scheduled agent. No-op in LOCAL_MODE."""
    from config import K8S_NAMESPACE, LOCAL_MODE  # noqa: PLC0415

    if LOCAL_MODE:
        return

    name = cronjob_name(site, agent_name, user_id)
    try:
        from kubernetes import client as k8s_client  # noqa: PLC0415
        from kubernetes import config as k8s_config  # noqa: PLC0415
        from kubernetes.client.rest import ApiException  # noqa: PLC0415

        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()

        batch_v1 = k8s_client.BatchV1Api()
        try:
            batch_v1.delete_namespaced_cron_job(name=name, namespace=K8S_NAMESPACE)
            log.info("Deleted CronJob %s (agent %s, user %s)", name, agent_name, user_id)
        except ApiException as exc:
            if exc.status == 404:
                log.debug("CronJob %s not found (never created or already deleted)", name)
            else:
                log.error("Failed to delete CronJob %s: HTTP %s %s", name, exc.status, exc.reason)
    except Exception as exc:
        log.error("K8s client error while deleting CronJob %s: %s", name, exc)
