"""ContentWriterAgent — updates a wiki page's content.md via natural language."""

from __future__ import annotations

from .base import BaseAgent, S3Tools


class ContentWriterAgent(BaseAgent):
    """Reads, edits, and saves a wiki page's content.md based on user instructions."""

    SYSTEM_PROMPT = """\
You are a wiki content editor. You will be given a user's editing instruction
and the current content of a wiki page in Markdown. Your job is to:

1. Call the get_content tool to retrieve the current page content.
2. Apply the requested changes to produce a complete updated Markdown document.
3. Call the put_content tool to save the updated content back to S3.
4. Reply with a brief friendly summary of the changes you made.

Current context:
  Site:      {site}
  Page path: {page_path}

Always preserve any content that the user did not ask to change.
When editing, produce the COMPLETE updated document, not just the changed section.
"""

    def __init__(self, s3_tools: S3Tools, model_id: str = "", **kwargs) -> None:
        from .base import DEFAULT_MODEL
        super().__init__(
            tools=[s3_tools.get_content, s3_tools.put_content],
            model_id=model_id or DEFAULT_MODEL,
            **kwargs,
        )
