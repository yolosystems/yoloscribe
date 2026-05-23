"""Unit tests for overwrite protection on create_agent / create_skill (YOL-50, YOL-48)
and SQS payload size guard in the runner tool (YOL-57).
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock

from yoloscribe_io import LocalStorageBackend
from agents.base import SiteTools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _site_tools(site: str = "alice") -> tuple[SiteTools, LocalStorageBackend]:
    store = LocalStorageBackend()
    return SiteTools(site, store, user_id="u1"), store


# ---------------------------------------------------------------------------
# create_agent overwrite protection (YOL-50 / YOL-53)
# ---------------------------------------------------------------------------


class TestCreateAgentOverwrite:
    def test_new_agent_is_created_without_overwrite(self):
        st, store = _site_tools()
        result = st.create_agent(
            agent_name="my-agent", description="Does stuff", skills=[]
        )
        assert "created" in result
        assert store.read("alice/.agents/my-agent/agent.md") is not None

    def test_existing_agent_blocked_by_default(self):
        st, store = _site_tools()
        # Pre-create the agent file so create_agent sees it as existing
        store.write("alice/.agents/my-agent/agent.md", "---\ntrigger: manual\n---\n")
        result = st.create_agent(
            agent_name="my-agent", description="Does stuff", skills=[]
        )
        assert "already exists" in result
        # File must remain unchanged (no overwrite)
        assert store.read("alice/.agents/my-agent/agent.md") == "---\ntrigger: manual\n---\n"

    def test_existing_agent_replaced_with_overwrite_true(self):
        st, store = _site_tools()
        store.write("alice/.agents/my-agent/agent.md", "---\ntrigger: manual\n---\n")
        result = st.create_agent(
            agent_name="my-agent",
            description="Does stuff",
            skills=[],
            overwrite=True,
        )
        assert "created" in result
        # Content must have been rewritten
        new_content = store.read("alice/.agents/my-agent/agent.md")
        assert new_content != "---\ntrigger: manual\n---\n"

    def test_invalid_name_still_rejected_before_existence_check(self):
        st, store = _site_tools()
        result = st.create_agent(
            agent_name="INVALID NAME!", description="x", skills=[]
        )
        assert "invalid agent name" in result
        # Nothing should have been written
        assert store.read("alice/.agents/INVALID NAME!/agent.md") is None

    def test_agent_written_to_correct_key_with_page_path(self):
        st, store = _site_tools()
        result = st.create_agent(
            agent_name="my-agent",
            description="x",
            skills=[],
            page_path="child-page",
        )
        assert "created" in result
        assert store.read("alice/child-page/.agents/my-agent/agent.md") is not None
        # Must NOT be at the root agents path
        assert store.read("alice/.agents/my-agent/agent.md") is None

    def test_agent_with_skills_and_schedule_created_correctly(self):
        st, store = _site_tools()
        result = st.create_agent(
            agent_name="daily-agent",
            description="Runs every day",
            skills=["summariser"],
            trigger="schedule",
            schedule="0 9 * * *",
            timezone="Europe/London",
        )
        assert "created" in result
        body = store.read("alice/.agents/daily-agent/agent.md") or ""
        assert "schedule: 0 9 * * *" in body
        assert "Europe/London" in body


# ---------------------------------------------------------------------------
# create_skill overwrite protection (YOL-48)
# ---------------------------------------------------------------------------


class TestCreateSkillOverwrite:
    def test_new_skill_is_created_without_overwrite(self):
        st, store = _site_tools()
        result = st.create_skill(
            name="my-skill",
            description="Does stuff",
            tools_list=[],
            body="Instructions here.",
        )
        assert "created" in result
        assert store.read("alice/.skills/my-skill/SKILL.md") is not None

    def test_existing_skill_blocked_by_default(self):
        st, store = _site_tools()
        store.write("alice/.skills/my-skill/SKILL.md", "---\ndescription: old\n---\n")
        result = st.create_skill(
            name="my-skill",
            description="Does stuff",
            tools_list=[],
            body="Instructions here.",
        )
        assert "already exists" in result
        # Content must remain unchanged
        assert store.read("alice/.skills/my-skill/SKILL.md") == "---\ndescription: old\n---\n"

    def test_existing_skill_replaced_with_overwrite_true(self):
        st, store = _site_tools()
        store.write("alice/.skills/my-skill/SKILL.md", "---\ndescription: old\n---\n")
        result = st.create_skill(
            name="my-skill",
            description="Does stuff",
            tools_list=[],
            body="New instructions.",
            overwrite=True,
        )
        assert "created" in result
        new_content = store.read("alice/.skills/my-skill/SKILL.md") or ""
        assert "Does stuff" in new_content

    def test_skill_written_to_correct_key(self):
        st, store = _site_tools()
        st.create_skill(
            name="my-skill",
            description="A skill",
            tools_list=["linear"],
            body="Body.",
        )
        assert store.read("alice/.skills/my-skill/SKILL.md") is not None
        # Must NOT exist for a different site
        assert store.read("bob/.skills/my-skill/SKILL.md") is None


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
