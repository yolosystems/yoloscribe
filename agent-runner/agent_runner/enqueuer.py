"""agent-enqueuer — lightweight SQS enqueuer for scheduled agent CronJobs.

Reads the standard agent-runner env vars and sends one SQS message to the
agent queue, then exits. The polling worker picks it up, acquires the page
lock, and dispatches a K8s Job to run the actual agent. This routes all
agent execution — both on_write and scheduled — through the same serialization
and lock-check logic.

Environment variables (all required unless noted):
    SQS_QUEUE_URL   SQS queue to write the job message to
    BUCKET          S3 bucket name
    AGENT_MD_KEY    S3 key for the agent.md file
    CONTENT_KEY     S3 key for the content.md file
    AGENT_PROMPT    Prompt string for the agent run
    USER_ID         User ID (optional; defaults to "default")
    AWS_REGION      AWS region (optional; defaults to "us-east-1")
    AWS_PROFILE     Named AWS profile (optional)
    SQS_ENDPOINT_URL  Custom SQS endpoint for local dev (optional)
"""

from __future__ import annotations

import json
import logging
import os

import boto3

from .log_setup import configure_logging

configure_logging()
log = logging.getLogger(__name__)


def main() -> None:
    sqs_queue_url = os.environ["SQS_QUEUE_URL"]
    bucket = os.environ["BUCKET"]
    agent_md_key = os.environ["AGENT_MD_KEY"]
    content_key = os.environ["CONTENT_KEY"]
    prompt = os.environ["AGENT_PROMPT"]
    user_id = os.environ.get("USER_ID", "default")
    aws_region = os.environ.get("AWS_REGION", "us-east-1")
    aws_profile = os.environ.get("AWS_PROFILE", "")
    sqs_endpoint_url = os.environ.get("SQS_ENDPOINT_URL", "")

    session = boto3.Session(profile_name=aws_profile or None)
    sqs_kwargs: dict = {"region_name": aws_region}
    if sqs_endpoint_url:
        sqs_kwargs["endpoint_url"] = sqs_endpoint_url
    sqs = session.client("sqs", **sqs_kwargs)

    log.info("Enqueuing scheduled agent job: %s", agent_md_key)
    sqs.send_message(
        QueueUrl=sqs_queue_url,
        MessageBody=json.dumps({
            "bucket": bucket,
            "agent_md_key": agent_md_key,
            "content_key": content_key,
            "prompt": prompt,
            "user_id": user_id,
        }),
    )
    log.info("Enqueued agent job for %s", agent_md_key)


if __name__ == "__main__":
    main()
