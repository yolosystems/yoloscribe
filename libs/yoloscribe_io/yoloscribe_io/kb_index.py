from __future__ import annotations

from .markdown_file import MarkdownFile
from .storage import StorageBackend


class KnowledgeBaseIndexMarkdownFile(MarkdownFile):
    """The site's knowledge base topic index at {site}/.user/ingest/content.md.

    The ingest page doubles as the topic index: bullet list items in the page
    are treated as topic names that map to top-level wiki pages. Example:

        - jazz
        - cooking
        - technology

    Site owners edit the ingest page directly to manage topics. The path is
    hardcoded so agents cannot be coerced into reading a different index.
    """

    def __init__(self, site: str, storage: StorageBackend) -> None:
        super().__init__(site, ".user/ingest/content.md", storage)

    @property
    def topics(self) -> list[str]:
        """Return the current list of topics parsed from the index file."""
        result = []
        for line in self.raw_content.splitlines():
            stripped = line.strip()
            if stripped.startswith(("- ", "* ", "+ ")):
                topic = stripped[2:].strip()
                if topic:
                    result.append(topic)
        return result

    def update_topics(self, topics: list[str]) -> None:
        """Replace the topic list entirely with the given topics."""
        content = "\n".join(f"- {t}" for t in topics)
        if content:
            content += "\n"
        self.write(content)
