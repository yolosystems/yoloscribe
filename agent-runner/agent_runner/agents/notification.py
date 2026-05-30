"""NotificationAgent — handles on_notify triggers."""

from __future__ import annotations

import logging
from typing import Callable

from strands_tools import http_request
from yoloscribe_io import AgentDefinition

from .base import BaseAgent
from .search import SearchBackend

log = logging.getLogger(__name__)


class NotificationAgent(BaseAgent):
    """Agent that fires on notification events.

    Tool surface:
    - http_request + any injected MCP tools (typically third-party integrations)

    NotificationAgent cannot read or write any wiki pages.  Its purpose is to
    dispatch notifications to external systems (e.g. post to Slack, create a
    Linear issue) using skills configured in .tools/.
    """

    def __init__(
        self,
        agent_def: AgentDefinition,
        site: str,
        page_path: str,
        storage,
        mcp_tools: list,
        model,
        user_id: str,
        notify_fn: Callable[[str, dict, str], None],
        search: SearchBackend | None = None,
        max_page_reads: int = 10,
    ) -> None:
        super().__init__(
            agent_def=agent_def,
            site=site,
            page_path=page_path,
            storage=storage,
            mcp_tools=mcp_tools,
            model=model,
            user_id=user_id,
            notify_fn=notify_fn,
            search=search,
            max_page_reads=max_page_reads,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        return (
            (self.agent_def.description or "")
            + "\n\nUse your tools to dispatch this notification. "
            "When done, output a brief confirmation of what was dispatched."
        )

    def run(self, prompt: str) -> None:
        tools = [http_request] + self._mcp_tools
        agent = self._make_strands_agent(tools)
        task = prompt.strip() or "Process this notification according to your instructions."
        agent(task)
