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

**Writing to memory:**
- When the user explicitly states a persistent preference, call write_memory
  immediately with level=explicit. Do not wait for a signal.
- When you can derive a conclusion that necessarily follows from an explicit
  premise already in memory PLUS observable page structure (path, content),
  call write_memory with level=deductive and set derived_from to the id(s) of
  the explicit premise(s) it follows from.
- Never write inductive or abductive conclusions via write_memory — those are
  reserved for the background consolidation pass.
- Use a unique id of the form c-{6 random lowercase hex chars}.
- statement must be ≤ 500 characters and fully self-contained.

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

    @tool
    def write_memory(conclusions: list[dict]) -> str:
        """Write explicit or deductive conclusions to the Librarian memory file.

        Use this to persist conclusions derived during this conversation:
        - level=explicit: the user directly stated a persistent preference
        - level=deductive: necessarily follows from an explicit premise already
          in memory plus observable page structure; set derived_from accordingly

        Never write inductive or abductive conclusions here — those are derived
        by the background MemoryReasoner from signal patterns.

        Conclusions are merged by id — an existing id updates that conclusion.
        The scaffolding rule is enforced: derived_from may only reference
        conclusions of equal or higher certainty.

        Args:
            conclusions: List of conclusion dicts. Required fields: id (c-xxxxxx),
                level (explicit|deductive), domain (ingest|enrich|retrieve|
                notify|present), statement (≤500 chars). Optional: evidence,
                derived_from, status (default active).
        """
        from yoloscribe_io import Conclusion, EvidenceEntry, MemoryFile

        if not conclusions:
            return "No conclusions provided."
        mf = MemoryFile(site=site, storage=storage)
        parsed: list[Conclusion] = []
        parse_errors: list[str] = []
        for raw in conclusions:
            if not isinstance(raw, dict):
                parse_errors.append(f"Skipped non-dict entry: {raw!r:.80}")
                continue
            cid = str(raw.get("id", "")).strip()
            if not cid:
                parse_errors.append("Conclusion missing required 'id'")
                continue
            level = str(raw.get("level", "")).strip()
            if level not in ("explicit", "deductive"):
                parse_errors.append(
                    f"'{cid}': level must be 'explicit' or 'deductive' "
                    f"(got '{level}') — inductive/abductive are reserved for the background pass"
                )
                continue
            domain = str(raw.get("domain", "")).strip()
            if domain not in ("ingest", "enrich", "retrieve", "notify", "present"):
                parse_errors.append(f"'{cid}': invalid domain '{domain}'")
                continue
            statement = str(raw.get("statement", "")).strip()
            if not statement:
                parse_errors.append(f"'{cid}': missing statement")
                continue
            if len(statement) > 500:
                parse_errors.append(f"'{cid}': statement exceeds 500 chars ({len(statement)})")
                continue
            evidence = [
                EvidenceEntry(
                    type=str(e.get("type", "")),
                    ref=str(e.get("ref", "")),
                    at=str(e.get("at", "")),
                    note=str(e.get("note", "")),
                )
                for e in (raw.get("evidence") or [])
                if isinstance(e, dict)
            ]
            parsed.append(Conclusion(
                id=cid,
                level=level,
                domain=domain,
                statement=statement,
                evidence=evidence,
                derived_from=[str(x) for x in (raw.get("derived_from") or [])],
                status=str(raw.get("status", "active")),
            ))
        if not parsed and parse_errors:
            return "All conclusions rejected:\n" + "\n".join(f"- {e}" for e in parse_errors)
        try:
            created, updated, rejected = mf.upsert(parsed)
        except Exception as exc:
            logger.warning("write_memory failed for site %s: %s", site, exc)
            return f"Memory write failed: {exc}"
        parts = []
        if created:
            parts.append(f"{created} created")
        if updated:
            parts.append(f"{updated} updated")
        result = "Memory updated: " + ", ".join(parts) + "." if parts else "No changes written."
        if rejected or parse_errors:
            all_rejected = rejected + parse_errors
            result += " Rejected: " + "; ".join(all_rejected)
        return result

    return [write_signal, read_archetypes, write_archetypes, write_memory]
