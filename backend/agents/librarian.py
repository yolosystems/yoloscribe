"""LibrarianAgent — extends ChatAgent with per-user preference memory (Phase 1)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from strands import tool

from .chat import ChatAgent

if TYPE_CHECKING:
    import mypy_boto3_s3
    import mypy_boto3_sqs

logger = logging.getLogger(__name__)

_LIBRARIAN_ADDENDUM = """
## Librarian Memory

The following conclusions have been derived from this user's interaction history.
They are certainty-scaffolded — use them accordingly:

- **explicit** (directly stated by user) / **deductive** (necessarily follows):
  act on these directly when relevant. They reflect high-confidence preferences.
- **inductive** / **abductive** (pattern-inferred): surface as polite, batched
  suggestions only. NEVER act autonomously on inductive/abductive conclusions.
  Maximum one suggestion per response. Do not repeat a suggestion the user has
  already declined in this conversation.

When a user **confirms** an inductive suggestion: proceed with the action, then
call write_signal with type=proposal_accepted and a brief description in the
payload so future reasoning can reinforce the conclusion.

When a user **declines** a suggestion: acknowledge politely and call write_signal
with type=proposal_rejected so the conclusion can be reconsidered.

Before calling the creator tool to define a new agent, always call read_archetypes
first. If a matching archetype already exists (same purpose or skills), reuse or
tune it rather than minting a duplicate. After provisioning a genuinely new agent
type, call write_archetypes to record the updated archetype index.

Active conclusions:

{memory_yaml}
"""


class LibrarianAgent(ChatAgent):
    """Extends ChatAgent with per-user preference memory and archetype dedup.

    Loaded as the /chat entry point in place of ChatAgent. No behaviour change
    for users with no memory (the addendum is omitted when memory.md is empty).
    """

    def _extra_system_context(self, site: str) -> str:
        import yaml
        from yoloscribe_io import MemoryFile, conclusion_to_dict

        try:
            mf = MemoryFile(site=site, storage=self._storage)
            _, conclusions = mf.read()
            active = [c for c in conclusions if c.status == "active"]
            if not active:
                return ""
            memory_yaml = yaml.dump(
                [conclusion_to_dict(c) for c in active],
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
            return _LIBRARIAN_ADDENDUM.format(memory_yaml=memory_yaml)
        except Exception as exc:
            logger.warning("LibrarianAgent: failed to load memory for site %s: %s", site, exc)
            return ""

    def _make_tools(
        self,
        site: str,
        page_path: str,
        file_path: str = "content.md",
        shared: dict | None = None,
        user_id: str = "knuth",
    ) -> list:
        base_tools = super()._make_tools(
            site=site,
            page_path=page_path,
            file_path=file_path,
            shared=shared,
            user_id=user_id,
        )
        storage = self._storage
        librarian_tools = _make_librarian_tools(site=site, storage=storage)
        return base_tools + librarian_tools


def _make_librarian_tools(site: str, storage) -> list:
    """Build the Librarian-specific strands tools bound to site/storage."""

    @tool
    def write_signal(signal_type: str, payload: dict | None = None) -> str:
        """Append a preference signal to the Librarian signal log.

        Call this when the user accepts or rejects a suggestion so future memory
        reasoning can reinforce or decay the underlying conclusion.

        Args:
            signal_type: One of: proposal_accepted, proposal_rejected,
                user_instruction.
            payload: Optional dict with context, e.g. {"suggestion": "..."}.
        """
        from yoloscribe_io import SignalEntry, SignalLog

        try:
            sl = SignalLog(site=site, storage=storage)
            sl.append(SignalEntry(type=signal_type, payload=payload or {}))
            return f"Signal '{signal_type}' recorded."
        except Exception as exc:
            logger.warning("write_signal failed: %s", exc)
            return f"Signal could not be recorded: {exc}"

    @tool
    def read_archetypes() -> str:
        """Read the archetype index for this site.

        Returns the raw markdown archetype index. Call this before creating any
        new agent to check whether a suitable template already exists. If an
        archetype matches (same purpose or skills), reuse or tune it instead of
        minting a duplicate agent.
        """
        from yoloscribe_io import ArchetypeFile

        try:
            af = ArchetypeFile(site=site, storage=storage)
            content = af.read()
            return content or "No archetypes defined yet."
        except Exception as exc:
            logger.warning("read_archetypes failed: %s", exc)
            return f"Could not read archetypes: {exc}"

    @tool
    def write_archetypes(content: str) -> str:
        """Write the updated archetype index for this site.

        Call this after provisioning a new agent type to record the template.
        The content is the full updated markdown for .user/librarian/archetypes.md.
        Preserve all existing entries and add the new archetype.

        Args:
            content: Full updated Markdown for the archetypes file.
        """
        from yoloscribe_io import ArchetypeFile

        if not content.strip():
            return "Error: content must not be empty."
        try:
            af = ArchetypeFile(site=site, storage=storage)
            af.write(content)
            return "Archetypes updated."
        except Exception as exc:
            logger.warning("write_archetypes failed: %s", exc)
            return f"Could not write archetypes: {exc}"

    return [write_signal, read_archetypes, write_archetypes]
