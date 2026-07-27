"""Tests for polling_worker's run-token mint wiring (YOL-502 increment 4)."""
from __future__ import annotations

import os

# polling_worker reads SQS_QUEUE_URL at import (see conformance/conftest.py).
os.environ.setdefault("SQS_QUEUE_URL", "http://localhost:9324/000000000000/q")

import httpx  # noqa: E402

from agent_runner import polling_worker  # noqa: E402
from tests.conftest import make_def  # noqa: E402


def _payload(agent_md_key: str, content_key: str, user_id: str = "u1") -> dict:
    return {
        "agent_md_key": agent_md_key,
        "content_key": content_key,
        "user_id": user_id,
        "bucket": "b",
        "prompt": "p",
    }


class TestRunScope:
    def test_page_agent_scope(self):
        payload = _payload("s/projects/x/.agents/a/agent.md", "s/projects/x/content.md")
        assert polling_worker._run_scope(payload, make_def(trigger="on_write", type="page")) == (
            "s", "page", "projects/x",
        )

    def test_ingest_scope(self):
        payload = _payload("s/.user/ingest/.agents/ing/agent.md", "s/.user/ingest/content.md")
        assert polling_worker._run_scope(payload, make_def(trigger="schedule", type="ingest")) == (
            "s", "ingest", ".user/ingest",
        )

    def test_notification_scope_from_trigger(self):
        payload = _payload("s/.agents/notifier/agent.md", "s/.user/notifications.md")
        site, agent_type, _ = polling_worker._run_scope(payload, make_def(trigger="on_notify", type="notification"))
        assert (site, agent_type) == ("s", "notification")


class TestMint:
    def _enable_mcp(self, monkeypatch):
        monkeypatch.setattr(polling_worker, "AGENT_RUNNER_ACCESS", "mcp")
        monkeypatch.setattr(polling_worker, "MINT_API_BASE", "http://backend:8000")
        monkeypatch.setattr(polling_worker, "INTERNAL_MINT_SECRET", "secret")

    def test_no_mint_when_access_is_s3(self, monkeypatch):
        monkeypatch.setattr(polling_worker, "AGENT_RUNNER_ACCESS", "s3")
        payload = _payload("s/p/.agents/a/agent.md", "s/p/content.md")
        polling_worker._mint_run_credentials(payload, make_def(type="page"))
        assert "_run_token" not in payload and "_mcp_url" not in payload

    def test_mint_stashes_token_and_url_with_correct_request(self, monkeypatch):
        self._enable_mcp(monkeypatch)
        captured: dict = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"token": "tok", "mcp_url": "http://backend:8000/mcp/v1"}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(url=url, headers=headers, json=json)
            return _Resp()

        monkeypatch.setattr(httpx, "post", fake_post)
        payload = _payload("s/projects/x/.agents/a/agent.md", "s/projects/x/content.md")
        polling_worker._mint_run_credentials(payload, make_def(trigger="on_write", type="page"))

        assert payload["_run_token"] == "tok"
        assert payload["_mcp_url"] == "http://backend:8000/mcp/v1"
        assert captured["url"] == "http://backend:8000/internal/runs/mint"
        assert captured["headers"]["X-Internal-Auth"] == "secret"
        assert captured["json"] == {
            "site": "s", "user_id": "u1", "agent_name": "a",
            "agent_type": "page", "page_path": "projects/x",
        }

    def test_mint_failure_falls_back_to_s3(self, monkeypatch):
        self._enable_mcp(monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(httpx, "post", boom)
        payload = _payload("s/p/.agents/a/agent.md", "s/p/content.md")
        polling_worker._mint_run_credentials(payload, make_def(type="page"))  # must not raise
        assert "_run_token" not in payload  # fell back to the legacy s3 path
