"""RunnerAgent — queues an agent.md-defined agent for async execution via SQS.

NOTE: RunnerAgent is not currently used. The ChatAgent runner tool sends SQS
messages directly. This class is retained for potential future use.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from strands import tool

from .base import BaseAgent, S3Tools, agents_prefix

if TYPE_CHECKING:
    import mypy_boto3_sqs


class RunnerAgent(BaseAgent):
    """Queues a page-level agent for asynchronous execution via SQS."""

    SYSTEM_PROMPT = """\
You are an agent-runner assistant for AgentScribe.

Your job is to queue a named agent (defined in an agent.md file) for asynchronous
execution. You do this by:
1. Confirming which agent the user wants to run (call list_agents if unclear).
2. Confirming the prompt / task to pass to that agent.
3. Calling queue_agent_run to dispatch the job.
4. Replying with a confirmation that the agent has been queued.

Current context:
  Site:      {site}
  Page path: {page_path}
"""

    def __init__(
        self,
        s3_tools: S3Tools,
        sqs_queue_url: str,
        sqs_client: "mypy_boto3_sqs.SQSClient",
        model_id: str = "",
        user_id: str = "knuth",
        **kwargs,
    ) -> None:
        from .base import DEFAULT_MODEL

        queue_tool = _make_queue_tool(
            s3_tools=s3_tools,
            sqs_client=sqs_client,
            sqs_queue_url=sqs_queue_url,
            user_id=user_id,
        )
        super().__init__(
            tools=[s3_tools.list_agents, queue_tool],
            model_id=model_id or DEFAULT_MODEL,
            **kwargs,
        )


def _make_queue_tool(
    s3_tools: S3Tools,
    sqs_client: "mypy_boto3_sqs.SQSClient",
    sqs_queue_url: str,
    user_id: str = "knuth",
):
    @tool
    def queue_agent_run(
        site: str,
        page_path: str,
        agent_name: str,
        prompt: str,
    ) -> str:
        """Queue a named page-level agent for asynchronous execution via SQS.

        Args:
            site: The site name (top-level S3 prefix).
            page_path: Relative page path; empty for root.
            agent_name: Name of the agent to run.
            prompt: The task or instruction to pass to the agent.
        """
        prefix = agents_prefix(site, page_path)
        agent_md_key = f"{prefix}/{agent_name}/agent.md"
        content_key = f"{site}/{page_path}/content.md" if page_path else f"{site}/content.md"

        payload = {
            "bucket": s3_tools.bucket,
            "content_key": content_key,
            "agent_md_key": agent_md_key,
            "prompt": prompt,
            "user_id": user_id,
        }
        sqs_client.send_message(
            QueueUrl=sqs_queue_url,
            MessageBody=json.dumps(payload),
        )
        return f"Agent '{agent_name}' queued successfully for page '{page_path or site}'."

    return queue_agent_run
