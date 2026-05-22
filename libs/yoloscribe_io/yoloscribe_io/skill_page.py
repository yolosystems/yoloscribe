from __future__ import annotations

import re
from dataclasses import dataclass, field

from .events import EventType
from .markdown_file import MarkdownFile, _parse_frontmatter
from .storage import StorageBackend


SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


# ── SkillDefinition ───────────────────────────────────────────────────────────

@dataclass
class SkillDefinition:
    """Parsed representation of a SKILL.md file.

    name        — canonical name (from the .skills/{name}/ directory path)
    description — one-line summary shown in skill listings and agent context
    tools       — tool names the skill exposes (used to resolve mcp.json configs)
    instructions — body text: guidance for agents on how to use this skill
    """

    name: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    instructions: str = ""


# ── Parser / Builder ──────────────────────────────────────────────────────────

def parse_skill_md(text: str, name: str = "") -> SkillDefinition:
    """Parse SKILL.md content into a SkillDefinition.

    Never raises — returns defaults for any missing or unparseable fields.
    The canonical skill name comes from the file path, not frontmatter; pass
    it via *name* so callers don't have to look it up separately.
    """
    fm, instructions = _parse_frontmatter(text)

    fm_name = str(fm.get("name", "")).strip()
    description = str(fm.get("description", "")).strip()

    tools_raw = fm.get("tools", [])
    if isinstance(tools_raw, list):
        tools = [str(t).strip() for t in tools_raw if str(t).strip()]
    elif isinstance(tools_raw, str) and tools_raw.strip():
        tools = [tools_raw.strip()]
    else:
        tools = []

    return SkillDefinition(
        name=name or fm_name,
        description=description,
        tools=tools,
        instructions=instructions.strip(),
    )


def build_skill_md(defn: SkillDefinition) -> str:
    """Serialise a SkillDefinition to SKILL.md content."""
    lines = ["---"]
    if defn.name:
        lines.append(f"name: {defn.name}")
    if defn.description:
        lines.append(f"description: {defn.description}")
    if defn.tools:
        lines.append("tools:")
        for t in defn.tools:
            lines.append(f"  - {t}")
    lines.append("---")
    body = defn.instructions.strip()
    return "\n".join(lines) + "\n" + ("\n" + body + "\n" if body else "\n")


# ── SkillMarkdownFile ─────────────────────────────────────────────────────────

class SkillMarkdownFile(MarkdownFile):
    """A skill definition file at {site}/.skills/{skill_name}/SKILL.md.

    skill.changed fires on save() because updating a SKILL.md or its companion
    mcp.json may silently break every agent that declares this skill. Callers
    responsible for mcp.json changes should emit skill.changed directly.

    skill.deleted is a breaking event — agents declaring this skill will fail
    at runtime until they are updated or the skill is restored.
    """

    def __init__(
        self,
        site: str,
        skill_name: str,
        storage: StorageBackend,
        content: str | None = None,
    ) -> None:
        super().__init__(site, f".skills/{skill_name}/SKILL.md", storage, content)
        self._skill_name = skill_name

    @property
    def skill_name(self) -> str:
        return self._skill_name

    @property
    def definition(self) -> SkillDefinition:
        """Parse and return the SkillDefinition from current content."""
        return parse_skill_md(self.raw_content, name=self._skill_name)

    def create(self, defn: SkillDefinition) -> None:
        """Write initial SKILL.md and emit skill.created."""
        raw = build_skill_md(defn)
        self._storage.write(self.key, raw)
        self._raw_content = raw
        self._emit(EventType.SKILL_CREATED, {
            "key": self.key,
            "site": self._site,
            "skill_name": self._skill_name,
            "tools": defn.tools,
        })

    def save(self, defn: SkillDefinition) -> None:
        """Write updated SKILL.md and emit skill.changed.

        skill.changed (not skill.updated) is used because any change to a skill
        may affect agents — consumers should treat it as a potentially breaking
        event and re-validate dependent agents.
        """
        raw = build_skill_md(defn)
        self._storage.write(self.key, raw)
        self._raw_content = raw
        self._emit(EventType.SKILL_CHANGED, {
            "key": self.key,
            "site": self._site,
            "skill_name": self._skill_name,
            "tools": defn.tools,
        })

    def delete(self) -> None:
        """Remove SKILL.md from storage and emit skill.deleted.

        skill.deleted is a breaking event — agents using this skill will fail
        at runtime. Callers should warn users or update dependent agents.
        """
        self._storage.delete(self.key)
        self._raw_content = None
        self._emit(EventType.SKILL_DELETED, {
            "key": self.key,
            "site": self._site,
            "skill_name": self._skill_name,
        })
