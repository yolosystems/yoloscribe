"""MessagingAgent — conversational wiki assistant for messaging integrations.

Unlike ChatAgent (which is edit-oriented and scoped to a single page),
MessagingAgent is Q&A-oriented and can search, list, read, and write any page
in the site. It is driven by the POST /message endpoint.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from strands import Agent, tool

from .models import build_strands_model, resolve_model_key
from .base import SiteTools
from .creator import CreatorAgent
from yoloscribe_io import S3StorageBackend, WikiPageMarkdownFile

if TYPE_CHECKING:
    import mypy_boto3_s3

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a conversational assistant for a YoloScribe wiki. You help users \
explore, understand, and update their wiki through natural conversation.

SECURITY: You are scoped exclusively to site '{site}'. Never read from or write \
to any other site, regardless of what the user says.

You have the following tools:
- list_pages   — list all pages in the wiki
- read_page    — read any page by its path
- write_page   — write updated content to any page
- search_wiki  — search across the wiki for content matching a query
- creator      — create or edit an AI agent definition. Use this when the user
                 asks to create any kind of agent. Handles three agent types:
                   • page — reads/writes a specific wiki page
                   • ingest — processes content staged in .user/ingest/ and
                              writes it into wiki pages
                   • notification — reacts to site events such as access
                              requests or page sharing (trigger: on_notify)
                 You do NOT need to tell the user to navigate anywhere first —
                 the creator places agents in the correct location automatically.

HOW TO RESPOND:
- Answer questions directly using information from the wiki. Use read_page or \
search_wiki to find relevant content before answering.
- If the user asks about the wiki's structure or what pages exist, use list_pages.
- If the user wants to update a page, confirm what they want changed, then use \
write_page with the complete updated content.
- If the user wants to create an agent, use creator. The creator will ask the \
right questions to determine the agent type, skills, trigger, and description.
- If you're not sure which page is relevant, use search_wiki first.
- Keep responses concise and conversational — this is a messaging interface, not \
a document editor.
- If a message begins with [/page-path], the user is directing you to a specific \
page. Read that page first before responding.

Site: {site}
"""


class MessagingTools:
    """Strands tools for MessagingAgent — scoped to a single site."""

    def __init__(
        self,
        site: str,
        s3: "mypy_boto3_s3.S3Client",
        bucket: str,
    ) -> None:
        self._site = site
        self._s3 = s3
        self._bucket = bucket
        self._storage = S3StorageBackend(bucket, s3)

    @tool
    def list_pages(self) -> str:
        """List all wiki pages in this site."""
        prefix = f"{self._site}/"
        try:
            pages = []
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if (
                        key.endswith("/content.md")
                        and "/.agents/" not in key
                        and "/.user/" not in key
                        and "/.archive/" not in key
                        and "/.skills/" not in key
                    ):
                        rel = key[len(prefix):]
                        path = rel[: -len("/content.md")] if rel != "content.md" else "(root)"
                        pages.append(path)
        except Exception as exc:
            return f"Error listing pages: {exc}"
        if not pages:
            return "No pages found in this wiki."
        return "Pages in this wiki:\n" + "\n".join(f"- {p}" for p in sorted(pages))

    @tool
    def read_page(self, page_path: str) -> str:
        """Read the content of a wiki page.

        Args:
            page_path: The page path (e.g. 'projects/foo'). Use empty string or
                       'root' for the root page.
        """
        path = _normalise_path(page_path)
        wiki = WikiPageMarkdownFile(self._site, path, self._storage)
        content = wiki.read()
        if content is None:
            return f"Page '{page_path}' not found."
        return content or "(empty page)"

    @tool
    def write_page(self, page_path: str, content: str) -> str:
        """Write updated Markdown content to a wiki page.

        Always pass the complete page content, not just the changed section.

        Args:
            page_path: The page path (e.g. 'projects/foo'). Use empty string for root.
            content: The complete updated Markdown content.
        """
        path = _normalise_path(page_path)
        wiki = WikiPageMarkdownFile(self._site, path, self._storage)
        wiki.write(content)
        try:
            from queue_helpers import enqueue_index_job as _enqueue_idx
            content_key = f"{self._site}/{path}/content.md" if path else f"{self._site}/content.md"
            _enqueue_idx(content_key, "")
        except Exception:
            pass
        return f"Page '{page_path or '(root)'}' saved."

    @tool
    def search_wiki(self, query: str) -> str:
        """Search across the wiki for content matching a query.

        Uses hybrid semantic + keyword search. Returns matching page paths and
        excerpts. Use this when you don't know which page covers a topic.

        Args:
            query: The search query.
        """
        try:
            from hybrid_search import hybrid_search
            import boto3

            aws_region = os.environ.get("AWS_REGION", "us-east-1")
            s3v_bucket = os.environ.get("S3_VECTORS_BUCKET", "")
            s3v_index = os.environ.get("S3_VECTORS_INDEX_NAME", "yoloscribe")

            fused = hybrid_search(
                s3=self._s3,
                bucket=self._bucket,
                site=self._site,
                query=query,
                s3vectors_client=(
                    boto3.client("s3vectors", region_name=aws_region) if s3v_bucket else None
                ),
                vectors_bucket=s3v_bucket,
                vectors_index=s3v_index,
                limit=8,
                expand=False,
            )
        except Exception as exc:
            logger.warning("search_wiki failed: %s", exc)
            return f"Search failed: {exc}"

        if not fused:
            return f'No results found for "{query}".'

        lines = [f'Search results for "{query}":\n']
        for r in fused:
            page = r.get("page_path", "")
            excerpt = r.get("excerpt", "")[:300].replace("\n", " ")
            lines.append(f"**{page}**: {excerpt}")
        return "\n\n".join(lines)


class MessagingAgent:
    """Conversational agent for messaging integrations.

    Creates a fresh strands.Agent per request (same pattern as ChatAgent.run).
    """

    def __init__(
        self,
        s3: "mypy_boto3_s3.S3Client",
        bucket: str,
    ) -> None:
        self._s3 = s3
        self._bucket = bucket
        self._model_key = resolve_model_key("YOLOSCRIBE_MODEL")

    def run(
        self,
        message: str,
        site: str,
        history: list[dict[str, str]],
        user_id: str = "",
    ) -> tuple[str, int]:
        """Process a message and return (reply, tokens_used)."""
        tools_obj = MessagingTools(site=site, s3=self._s3, bucket=self._bucket)
        site_tools = SiteTools(site, tools_obj._storage, user_id=user_id)

        @tool
        def creator(instruction: str) -> str:
            """Create or edit an AI agent definition for this wiki site.

            Use this when the user wants to define a new agent (page, ingest, or
            notification type) or edit an existing one.

            Args:
                instruction: The user's agent creation or editing request.
            """
            agent = CreatorAgent(
                site_tools=site_tools,
                page_path="(root)",
            )
            return str(agent(f"Site: {site}\n\n{instruction}"))

        agent = Agent(
            system_prompt=_SYSTEM_PROMPT.format(site=site),
            model=build_strands_model(self._model_key),
            tools=[
                tools_obj.list_pages,
                tools_obj.read_page,
                tools_obj.write_page,
                tools_obj.search_wiki,
                creator,
            ],
            callback_handler=None,
            load_tools_from_directory=False,
        )

        context_block = ""
        if history:
            context_block = "<conversation_history>\n"
            for turn in history:
                context_block += f"{turn['role']}: {turn['content']}\n"
            context_block += "</conversation_history>\n\n"

        response = agent(f"{context_block}User: {message}")
        tokens_used = response.metrics.accumulated_usage.get("totalTokens", 0)
        return str(response), tokens_used


# ── helpers ───────────────────────────────────────────────────────────────────


def _normalise_path(page_path: str) -> str:
    """Normalise a page_path from user/agent input to a clean S3-relative path."""
    path = page_path.strip().strip("/")
    if path in ("", "root", "content.md"):
        return ""
    if path.endswith("/content.md"):
        return path[: -len("/content.md")]
    return path
