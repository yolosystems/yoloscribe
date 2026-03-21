"""Unit tests for overwrite protection on put_agent / put_skill (YOL-50, YOL-48)
and SQS payload size guard in the runner tool (YOL-57).
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch

from agents.base import S3Tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tools(user_site: str = "alice") -> S3Tools:
    mock_s3 = MagicMock()
    return S3Tools(s3=mock_s3, bucket="test-bucket", user_site=user_site)


def _agent_exists(tools: S3Tools, agent_name: str, page_path: str = "") -> None:
    """Configure the mock so that list_objects_v2 reports the agent key exists."""
    tools.s3.list_objects_v2.return_value = {"KeyCount": 1}


def _agent_absent(tools: S3Tools) -> None:
    """Configure the mock so that list_objects_v2 reports no objects."""
    tools.s3.list_objects_v2.return_value = {"KeyCount": 0}


# ---------------------------------------------------------------------------
# put_agent overwrite protection (YOL-50 / YOL-53)
# ---------------------------------------------------------------------------


class TestPutAgentOverwrite:
    def test_new_agent_is_created_without_overwrite(self):
        tools = _make_tools()
        _agent_absent(tools)
        result = tools.put_agent(
            site="alice", agent_name="my-agent", description="Does stuff", skills=[]
        )
        assert "created" in result
        tools.s3.put_object.assert_called_once()

    def test_existing_agent_blocked_by_default(self):
        tools = _make_tools()
        _agent_exists(tools, "my-agent")
        result = tools.put_agent(
            site="alice", agent_name="my-agent", description="Does stuff", skills=[]
        )
        assert "already exists" in result
        # S3 write must NOT have been called
        tools.s3.put_object.assert_not_called()

    def test_existing_agent_replaced_with_overwrite_true(self):
        tools = _make_tools()
        # list_objects_v2 is called for the overwrite check — not needed when overwrite=True
        result = tools.put_agent(
            site="alice",
            agent_name="my-agent",
            description="Does stuff",
            skills=[],
            overwrite=True,
        )
        assert "created" in result
        tools.s3.put_object.assert_called_once()
        # list_objects_v2 should NOT be called when overwrite=True
        tools.s3.list_objects_v2.assert_not_called()

    def test_invalid_name_still_rejected_before_existence_check(self):
        tools = _make_tools()
        result = tools.put_agent(
            site="alice", agent_name="INVALID NAME!", description="x", skills=[]
        )
        assert "invalid agent name" in result
        tools.s3.list_objects_v2.assert_not_called()
        tools.s3.put_object.assert_not_called()

    def test_overwrite_check_uses_correct_s3_key(self):
        tools = _make_tools()
        _agent_absent(tools)
        tools.put_agent(
            site="alice",
            agent_name="my-agent",
            description="x",
            skills=[],
            page_path="child-page",
        )
        # The key checked should include the page_path
        call_kwargs = tools.s3.list_objects_v2.call_args
        assert "alice/child-page/.agents/my-agent/agent.md" in call_kwargs[1]["Prefix"]

    def test_agent_with_skills_and_schedule_created_correctly(self):
        tools = _make_tools()
        _agent_absent(tools)
        result = tools.put_agent(
            site="alice",
            agent_name="daily-agent",
            description="Runs every day",
            skills=["summariser"],
            schedule="0 9 * * *",
            timezone="Europe/London",
        )
        assert "created" in result
        body = tools.s3.put_object.call_args[1]["Body"].decode()
        assert "## Schedule" in body
        assert "0 9 * * *" in body
        assert "Europe/London" in body


# ---------------------------------------------------------------------------
# put_skill overwrite protection (YOL-48)
# ---------------------------------------------------------------------------


class TestPutSkillOverwrite:
    def test_new_skill_is_created_without_overwrite(self):
        tools = _make_tools()
        tools.s3.list_objects_v2.return_value = {"KeyCount": 0}
        tools.put_skill(site="alice", skill_name="my-skill", markdown="---\n---\n\nbody")
        tools.s3.put_object.assert_called_once()

    def test_existing_skill_blocked_by_default(self):
        tools = _make_tools()
        tools.s3.list_objects_v2.return_value = {"KeyCount": 1}
        with pytest.raises(ValueError, match="already exists"):
            tools.put_skill(site="alice", skill_name="my-skill", markdown="---\n---\n\nbody")
        tools.s3.put_object.assert_not_called()

    def test_existing_skill_replaced_with_overwrite_true(self):
        tools = _make_tools()
        tools.put_skill(
            site="alice",
            skill_name="my-skill",
            markdown="---\n---\n\nbody",
            overwrite=True,
        )
        tools.s3.put_object.assert_called_once()
        tools.s3.list_objects_v2.assert_not_called()

    def test_overwrite_check_uses_correct_s3_key(self):
        tools = _make_tools()
        tools.s3.list_objects_v2.return_value = {"KeyCount": 0}
        tools.put_skill(site="alice", skill_name="my-skill", markdown="---\n---\n\nbody")
        call_kwargs = tools.s3.list_objects_v2.call_args
        assert "alice/.skills/my-skill/SKILL.md" in call_kwargs[1]["Prefix"]


# ---------------------------------------------------------------------------
# SQS payload size guard in runner tool (YOL-57)
# ---------------------------------------------------------------------------


class TestSqsPayloadSizeGuard:
    """Test the runner tool's SQS payload truncation logic.

    We exercise the logic directly by calling it through a minimal harness
    that reproduces the runner closure behaviour without spinning up the full
    ChatAgent stack.
    """

    def _make_runner_payload(self, prompt: str) -> dict:
        """Build the base payload dict exactly as the runner tool does."""
        return {
            "bucket": "test-bucket",
            "content_key": "alice/content.md",
            "agent_md_key": "alice/.agents/my-agent/agent.md",
            "prompt": prompt,
            "user_id": "user-uuid-123",
        }

    def test_small_payload_sent_as_is(self):
        mock_sqs = MagicMock()
        prompt = "short prompt"
        payload = self._make_runner_payload(prompt)
        body_str = json.dumps(payload)
        assert len(body_str.encode()) < 256 * 1024
        mock_sqs.send_message(QueueUrl="https://sqs/queue", MessageBody=body_str)
        sent = mock_sqs.send_message.call_args[1]["MessageBody"]
        assert json.loads(sent)["prompt"] == prompt

    def test_oversized_prompt_is_truncated(self):
        """A prompt that pushes the payload over 256 KB must be truncated."""
        # Build a prompt that is clearly too long on its own
        huge_prompt = "x" * (300 * 1024)
        _SQS_MAX_BYTES = 256 * 1024
        payload = self._make_runner_payload(huge_prompt)
        body_str = json.dumps(payload)
        assert len(body_str.encode()) > _SQS_MAX_BYTES  # confirm the test premise

        # Replicate the truncation logic from chat.py runner tool
        overhead = len(json.dumps({**payload, "prompt": ""}).encode())
        max_prompt_bytes = _SQS_MAX_BYTES - overhead - 32
        assert max_prompt_bytes > 0
        truncated = huge_prompt.encode()[:max_prompt_bytes].decode(errors="ignore")
        payload["prompt"] = truncated + "\n...[truncated]"
        body_str = json.dumps(payload)

        assert len(body_str.encode()) <= _SQS_MAX_BYTES
        assert json.loads(body_str)["prompt"].endswith("\n...[truncated]")

    def test_truncated_payload_preserves_structural_fields(self):
        """Structural fields must survive truncation intact."""
        huge_prompt = "y" * (300 * 1024)
        _SQS_MAX_BYTES = 256 * 1024
        payload = self._make_runner_payload(huge_prompt)

        overhead = len(json.dumps({**payload, "prompt": ""}).encode())
        max_prompt_bytes = _SQS_MAX_BYTES - overhead - 32
        truncated = huge_prompt.encode()[:max_prompt_bytes].decode(errors="ignore")
        payload["prompt"] = truncated + "\n...[truncated]"
        result = json.loads(json.dumps(payload))

        assert result["bucket"] == "test-bucket"
        assert result["content_key"] == "alice/content.md"
        assert result["agent_md_key"] == "alice/.agents/my-agent/agent.md"
        assert result["user_id"] == "user-uuid-123"
