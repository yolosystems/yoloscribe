"""Base agent class for the agent runner OO hierarchy."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Callable

from yoloscribe_io import AgentDefinition

from .search import SearchBackend, NullSearchBackend

log = logging.getLogger(__name__)


class BaseAgent(ABC):
    """Abstract base for all agent types.

    Subclasses define the scope-limited tool surface presented to the LLM and
    implement the run() lifecycle.  Shared infrastructure (MCP tools, model,
    storage) is injected at construction so the subclass never re-creates it.

    Conventions:
    - Methods intended as strands tools are public and have full type
      annotations plus a one-line docstring (used as the tool description).
    - run() raises on fatal errors; callers handle notifications and logging.
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
        self.agent_def = agent_def
        self._site = site
        self._page_path = page_path
        self._storage = storage
        self._mcp_tools = mcp_tools
        self._model = model
        self._user_id = user_id
        self._notify = notify_fn
        self._search: SearchBackend = search or NullSearchBackend()
        self._max_page_reads = max_page_reads

    @abstractmethod
    def _build_system_prompt(self) -> str:
        """Return the system prompt for the underlying strands Agent."""
        ...

    @abstractmethod
    def run(self, prompt: str) -> int:
        """Execute the agent.  Returns total tokens consumed.  Raises on fatal errors."""
        ...

    def _make_strands_agent(self, tools: list):
        from strands import Agent, ModelRetryStrategy
        return Agent(
            system_prompt=self._build_system_prompt(),
            model=self._model,
            tools=tools,
            callback_handler=None,
            load_tools_from_directory=False,
            retry_strategy=ModelRetryStrategy(
                max_attempts=8,
                initial_delay=10,
                max_delay=120,
            ),
        )
