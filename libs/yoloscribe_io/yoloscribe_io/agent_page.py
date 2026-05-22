from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch

from .events import EventType
from .markdown_file import MarkdownFile, _parse_frontmatter
from .storage import StorageBackend


# ── Constants ─────────────────────────────────────────────────────────────────

AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_VALID_TRIGGERS = frozenset({"manual", "schedule", "on_write", "on_notify"})


# ── Error ─────────────────────────────────────────────────────────────────────

class AgentDefinitionError(ValueError):
    """Raised when an agent.md file is invalid or missing required fields."""


# ── Scope ─────────────────────────────────────────────────────────────────────

@dataclass
class Scope:
    """Glob-based page scope for cross-page agents.

    An empty include list means the agent watches all pages (no filter applied).
    Exclude patterns are evaluated after include and always win.
    """

    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    def matches(self, page_path: str) -> bool:
        """Return True if page_path is in scope (include match, not excluded)."""
        if self.include:
            if not any(fnmatch(page_path, pat) for pat in self.include):
                return False
        if any(fnmatch(page_path, pat) for pat in self.exclude):
            return False
        return True

    def to_dict(self) -> dict:
        d: dict = {}
        if self.include:
            d["include"] = list(self.include)
        if self.exclude:
            d["exclude"] = list(self.exclude)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Scope:
        return cls(
            include=list(d.get("include", [])),
            exclude=list(d.get("exclude", [])),
        )


# ── AgentDefinition ───────────────────────────────────────────────────────────

@dataclass
class AgentDefinition:
    name: str
    description: str = ""
    skills: list[str] = field(default_factory=list)
    trigger: str = "manual"
    schedule: str = ""
    timezone: str = ""
    model: str = ""
    confirm_before_write: bool = False
    ref: str = ""
    scope: Scope = field(default_factory=Scope)


# ── Parser ────────────────────────────────────────────────────────────────────

def parse_agent_md(text: str) -> AgentDefinition:
    """Parse an agent.md file into an AgentDefinition.

    Supports both the current frontmatter format and the legacy body-section
    format (# Agent: / ## Description / ## Skills / ## Model).

    Raises AgentDefinitionError on structural problems or constraint violations.
    """
    if not text.startswith("---"):
        raise AgentDefinitionError(
            "agent.md must begin with YAML frontmatter (---). "
            "Add a frontmatter block with at least 'trigger:' to fix this."
        )
    if text.find("\n---", 3) == -1:
        raise AgentDefinitionError(
            "agent.md frontmatter block is not closed (missing closing **---)."
        )

    try:
        fm, body = _parse_frontmatter(text)
    except Exception as exc:
        raise AgentDefinitionError(f"Failed to parse frontmatter: {exc}") from exc

    # ref — pointer agents delegate everything else to another agent.md
    ref = str(fm.get("ref", "")).strip()
    if ref:
        name = str(fm.get("name", "")).strip()
        trigger = str(fm.get("trigger", "on_write")).strip()
        return AgentDefinition(name=name, trigger=trigger, ref=ref)

    trigger = str(fm.get("trigger", "manual")).strip()
    if trigger not in _VALID_TRIGGERS:
        raise AgentDefinitionError(
            f"Invalid trigger '{trigger}'. "
            f"Must be one of: {', '.join(sorted(_VALID_TRIGGERS))}."
        )

    schedule = str(fm.get("schedule", "")).strip()
    if trigger == "schedule" and not schedule:
        raise AgentDefinitionError(
            "trigger: schedule requires a 'schedule' field (cron expression)."
        )

    timezone = str(fm.get("timezone", "")).strip()
    model = str(fm.get("model", "")).strip()
    confirm_before_write = bool(fm.get("confirm_before_write", False))

    name = str(fm.get("name", "")).strip()
    fm_skills_raw = fm.get("skills", [])
    fm_skills: list[str] = (
        [str(fm_skills_raw)] if isinstance(fm_skills_raw, str)
        else [str(s) for s in fm_skills_raw]
    )

    scope_raw = fm.get("scope", {})
    scope = Scope.from_dict(scope_raw) if isinstance(scope_raw, dict) else Scope()

    # Parse legacy body sections (## Description, ## Skills, ## Model)
    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    free_lines: list[str] = []

    for line in body.splitlines():
        if line.startswith("# Agent:"):
            if not name:
                name = line[len("# Agent:"):].strip()
        elif line.startswith("## "):
            current_section = line[3:].strip()
            sections.setdefault(current_section, [])
        elif current_section is not None:
            sections[current_section].append(line)
        else:
            free_lines.append(line)

    def _section_text(key: str) -> str:
        return "\n".join(sections.get(key, [])).strip()

    description = _section_text("Description") if "Description" in sections else "\n".join(free_lines).strip()

    skills = fm_skills or [
        ln[2:].strip()
        for ln in sections.get("Skills", [])
        if ln.startswith("- ")
    ]

    if not model:
        model = _section_text("Model")

    if not name:
        raise AgentDefinitionError(
            "agent.md must have a 'name' frontmatter field or a '# Agent: {name}' heading."
        )

    return AgentDefinition(
        name=name,
        description=description,
        skills=skills,
        trigger=trigger,
        schedule=schedule,
        timezone=timezone,
        model=model,
        confirm_before_write=confirm_before_write,
        scope=scope,
    )


# ── Builder ───────────────────────────────────────────────────────────────────

def build_agent_md(defn: AgentDefinition) -> str:
    """Serialise an AgentDefinition to agent.md content (frontmatter format)."""
    lines = ["---", f"trigger: {defn.trigger}"]
    if defn.name:
        lines.append(f"name: {defn.name}")
    if defn.ref:
        lines.append(f"ref: {defn.ref}")
    if defn.schedule:
        lines.append(f"schedule: {defn.schedule}")
    if defn.timezone:
        lines.append(f"timezone: {defn.timezone}")
    if defn.skills:
        lines.append("skills:")
        for s in defn.skills:
            lines.append(f"  - {s}")
    if defn.model:
        lines.append(f"model: {defn.model}")
    if defn.confirm_before_write:
        lines.append("confirm_before_write: true")
    scope_d = defn.scope.to_dict()
    if scope_d:
        lines.append("scope:")
        for key in ("include", "exclude"):
            if key in scope_d:
                lines.append(f"  {key}:")
                for pat in scope_d[key]:
                    lines.append(f"    - {pat}")
    lines.append("---")
    body = (defn.description or "").strip()
    return "\n".join(lines) + "\n" + ("\n" + body + "\n" if body else "\n")


# ── AgentMarkdownFile ─────────────────────────────────────────────────────────

class AgentMarkdownFile(MarkdownFile):
    """An agent definition file at {site}/{page_path}/.agents/{agent_name}/agent.md."""

    def __init__(
        self,
        site: str,
        page_path: str,
        agent_name: str,
        storage: StorageBackend,
        content: str | None = None,
    ) -> None:
        if page_path:
            path = f"{page_path}/.agents/{agent_name}/agent.md"
        else:
            path = f".agents/{agent_name}/agent.md"
        super().__init__(site, path, storage, content)
        self._page_path = page_path
        self._agent_name = agent_name

    @property
    def page_path(self) -> str:
        return self._page_path

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def definition(self) -> AgentDefinition:
        """Parse and return the AgentDefinition. Raises AgentDefinitionError if invalid."""
        return parse_agent_md(self.raw_content)

    def create(self, defn: AgentDefinition) -> None:
        """Write initial agent.md and emit agent.created."""
        raw = build_agent_md(defn)
        self._storage.write(self.key, raw)
        self._raw_content = raw
        self._emit(EventType.AGENT_CREATED, {
            "key": self.key,
            "site": self._site,
            "page_path": self._page_path,
            "agent_name": self._agent_name,
        })

    def save(self, defn: AgentDefinition) -> None:
        """Write updated agent.md and emit agent.updated."""
        raw = build_agent_md(defn)
        self._storage.write(self.key, raw)
        self._raw_content = raw
        self._emit(EventType.AGENT_UPDATED, {
            "key": self.key,
            "site": self._site,
            "page_path": self._page_path,
            "agent_name": self._agent_name,
        })

    def delete(self) -> None:
        """Remove agent.md from storage and emit agent.deleted."""
        self._storage.delete(self.key)
        self._raw_content = None
        self._emit(EventType.AGENT_DELETED, {
            "key": self.key,
            "site": self._site,
            "page_path": self._page_path,
            "agent_name": self._agent_name,
        })
