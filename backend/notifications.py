"""Notification helpers — write canonical entries to a site's notifications.md."""

from __future__ import annotations

import json

from yoloscribe_io import NotificationsMarkdownFile

from s3_storage import storage


def _make_sqs_enqueue(site: str):
    """Return an enqueue callable for NotificationsMarkdownFile, or None if SQS is unconfigured."""
    from config import S3_BUCKET, SQS_QUEUE_URL, sqs

    if sqs is None or not SQS_QUEUE_URL:
        return None

    def _enqueue(agent_md_key: str, notif_key: str, prompt: str, user_id: str) -> None:
        sqs.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps({
                "bucket": S3_BUCKET,
                "agent_md_key": agent_md_key,
                "content_key": notif_key,
                "prompt": prompt,
                "user_id": user_id,
            }),
        )

    return _enqueue


def write_notification(
    site: str,
    event_type: str,
    payload: dict[str, str],
    *,
    user_id: str = "",
) -> None:
    """Append a canonical notification entry to the site's notifications.md.

    Entry format:
        ## YYYY-MM-DD HH:MM UTC — {event_type}

        key: value
        ...

    agent_success and agent_failure events are written but never enqueue
    on_notify agents (loop guard — enforced by NotificationsMarkdownFile).
    """
    notif = NotificationsMarkdownFile(site, storage, enqueue=_make_sqs_enqueue(site))
    notif.notify(event_type, payload, user_id=user_id)
