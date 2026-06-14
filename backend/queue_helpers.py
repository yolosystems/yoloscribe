"""SQS queue helpers for agent-runner and indexing jobs."""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

_ON_WRITE_PATTERN = re.compile(r"^trigger:\s*on_write", re.MULTILINE)


def enqueue_on_write_agents(site: str, content_key: str, user_id: str) -> None:
    """Enqueue agent-runner jobs for any on_write agents subscribed to this page.

    Lists .agents/ under the written page's directory and queues a job via SQS
    for each agent.md with trigger: on_write. Best-effort; never raises.
    """
    from config import S3_BUCKET, SQS_QUEUE_URL, s3, sqs

    if sqs is None or not SQS_QUEUE_URL:
        return

    if not content_key.endswith("/content.md"):
        return
    page_dir = content_key[: -len("/content.md")]
    agents_prefix = f"{page_dir}/.agents/"

    try:
        resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=agents_prefix)
    except Exception:
        log.warning("Failed to list on_write agents for %s", content_key, exc_info=True)
        return

    for obj in resp.get("Contents", []):
        key = obj["Key"]
        if not key.endswith("/agent.md"):
            continue
        try:
            agent_text = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read().decode("utf-8")
        except Exception:
            log.warning("Failed to read agent.md at %s", key, exc_info=True)
            continue

        if not _ON_WRITE_PATTERN.search(agent_text):
            continue

        try:
            sqs.send_message(
                QueueUrl=SQS_QUEUE_URL,
                MessageBody=json.dumps({
                    "bucket": S3_BUCKET,
                    "agent_md_key": key,
                    "content_key": content_key,
                    "prompt": "A page in your scope has been updated. Review it and apply any necessary updates to your tracked pages.",
                    "user_id": user_id,
                }),
            )
            log.info("Enqueued on_write agent %s for %s", key, content_key)
        except Exception:
            log.warning("Failed to enqueue on_write agent %s", key, exc_info=True)


def enqueue_ingest_agents(site: str, user_id: str) -> None:
    """Enqueue agent-runner jobs for on_write agents subscribed to the ingest queue.

    Called when a file is uploaded to .user/ingest/ so the ingest agent starts
    immediately rather than waiting for its next scheduled run. Best-effort; never raises.
    """
    from config import S3_BUCKET, SQS_QUEUE_URL, s3, sqs

    if sqs is None or not SQS_QUEUE_URL:
        return

    agents_prefix = f"{site}/.user/ingest/.agents/"
    content_key = f"{site}/.user/ingest/content.md"

    try:
        resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=agents_prefix)
    except Exception:
        log.warning("Failed to list ingest agents for site %s", site, exc_info=True)
        return

    for obj in resp.get("Contents", []):
        key = obj["Key"]
        if not key.endswith("/agent.md"):
            continue
        try:
            agent_text = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read().decode("utf-8")
        except Exception:
            log.warning("Failed to read agent.md at %s", key, exc_info=True)
            continue

        if not _ON_WRITE_PATTERN.search(agent_text):
            continue

        try:
            sqs.send_message(
                QueueUrl=SQS_QUEUE_URL,
                MessageBody=json.dumps({
                    "bucket": S3_BUCKET,
                    "agent_md_key": key,
                    "content_key": content_key,
                    "prompt": "New files have been added to the ingest queue. Process all pending files now.",
                    "user_id": user_id,
                }),
            )
            log.info("Enqueued ingest agent %s for site %s", key, site)
        except Exception:
            log.warning("Failed to enqueue ingest agent %s", key, exc_info=True)


def enqueue_eval_annotator(site: str, page_path: str, run_log_key: str, user_id: str) -> None:
    """Enqueue the platform phoenix-annotator agent when a run log file is saved.

    Looks for a platform-provisioned phoenix-annotator agent.md co-located under
    the same .agents/ directory as the eval_log agent whose run log was just saved.
    Best-effort; never raises.
    """
    from config import S3_BUCKET, SQS_QUEUE_URL, s3, sqs

    if sqs is None or not SQS_QUEUE_URL:
        return

    agents_dir = f"{site}/{page_path}/.agents" if page_path else f"{site}/.agents"
    eval_agent_key = f"{agents_dir}/phoenix-annotator/agent.md"

    try:
        s3.head_object(Bucket=S3_BUCKET, Key=eval_agent_key)
    except Exception:
        return

    try:
        sqs.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps({
                "bucket": S3_BUCKET,
                "agent_md_key": eval_agent_key,
                "content_key": run_log_key,
                "prompt": "Process the run log annotations and submit to Phoenix.",
                "user_id": user_id,
            }),
        )
        log.info("Enqueued eval annotator for run log %s", run_log_key)
    except Exception:
        log.warning("Failed to enqueue eval annotator for %s", run_log_key, exc_info=True)


def enqueue_index_job(content_key: str, user_id: str) -> None:
    """Send an indexing job to the SQS indexing queue (best-effort; never raises)."""
    from config import S3_BUCKET, SQS_INDEXING_QUEUE_URL, sqs_indexing

    if sqs_indexing is None or not SQS_INDEXING_QUEUE_URL:
        return
    # Skip .user/ paths — system/staging content, not first-class wiki pages.
    if "/.user/" in f"/{content_key}":
        return
    try:
        sqs_indexing.send_message(
            QueueUrl=SQS_INDEXING_QUEUE_URL,
            MessageBody=json.dumps({"bucket": S3_BUCKET, "content_key": content_key, "user_id": user_id}),
        )
    except Exception:
        log.warning("Failed to enqueue indexing job for %s", content_key, exc_info=True)
