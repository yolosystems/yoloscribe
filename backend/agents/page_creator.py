"""PageCreatorAgent — creates new wiki pages (or child pages) in S3."""

from __future__ import annotations

from .base import BaseAgent, S3Tools


class PageCreatorAgent(BaseAgent):
    """Creates a new wiki page or child page at the correct S3 location."""

    SYSTEM_PROMPT = """\
You are a wiki page creation assistant for AgentScribe.

Your job is to create a new wiki page (or child page) in S3:
1. Ask for the page name if not provided. It must:
   - Use only lowercase letters, digits, hyphens, and underscores.
   - Start with a letter or digit.
   - Use forward slashes for nested paths (e.g. "guides/getting-started").
2. Confirm the full page path with the user before creating it.
3. Call create_page to create the page in S3 (empty content.md + .agents marker).
4. Reply with a confirmation and the URL to visit the new page.

Current context:
  Site:        {site}
  Parent page: {page_path}
"""

    def __init__(self, s3_tools: S3Tools, model_id: str = "", **kwargs) -> None:
        from .base import DEFAULT_MODEL
        super().__init__(
            tools=[s3_tools.create_page],
            model_id=model_id or DEFAULT_MODEL,
            **kwargs,
        )
