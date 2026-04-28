"""Standalone AgentDefinition dataclass and agent.md parser."""

from __future__ import annotations

import dataclasses
import re


class AgentDefinitionError(Exception):
    """Raised when an agent.md file is invalid or missing required frontmatter."""


@dataclasses.dataclass
class AgentDefinition:
    name: str
    description: str
    skills: list[str]
    trigger: str = "manual"
    scope: list[str] = dataclasses.field(default_factory=list)
    ref: str = ""
    schedule: str = ""
    timezone: str = ""
    model: str = ""


_VALID_TRIGGERS = frozenset({"manual", "schedule", "on_write"})


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML frontmatter and remaining body.

    Returns (frontmatter_dict, body_text). Raises AgentDefinitionError if
    no valid frontmatter block is present.
    """
    if not text.startswith("---"):
        raise AgentDefinitionError(
            "agent.md must begin with YAML frontmatter (---). "
            "Add a frontmatter block with at least 'trigger:' to fix this."
        )

    end = text.find("\n---", 3)
    if end == -1:
        raise AgentDefinitionError(
            "agent.md frontmatter block is not closed (missing closing ---)."
        )

    fm_lines = text[3:end].splitlines()
    body = text[end + 4:].lstrip("\n")

    result: dict = {}
    current_list_key: str | None = None

    for line in fm_lines:
        if current_list_key is not None and re.match(r"^\s+-\s", line):
            result[current_list_key].append(line.strip()[2:].strip())
            continue

        current_list_key = None

        if ":" not in line:
            continue

        key, _, raw_value = line.partition(":")
        key = key.strip()
        value = raw_value.strip()

        if value:
            result[key] = value
        else:
            result[key] = []
            current_list_key = key

    return result, body


def parse_agent_md(text: str) -> AgentDefinition:
    """Parse an agent.md file into an AgentDefinition.

    Requires YAML frontmatter. Raises AgentDefinitionError on missing or
    invalid frontmatter, or when required field constraints are violated.

    Frontmatter fields:
        trigger:   manual | schedule | on_write  (default: manual)
        scope:     list of glob patterns          (default: [])
        ref:       S3 key to upstream agent.md    (pointer agents only)
        schedule:  cron expression                (required if trigger: schedule)
        timezone:  TZ database name               (optional)
        model:     model registry key             (optional)

    Body sections:
        # Agent: {name}
        ## Description
        ## Skills
        ## Model  (overridden by frontmatter 'model' if both present)
    """
    try:
        fm, body = _parse_frontmatter(text)
    except AgentDefinitionError:
        raise
    except Exception as exc:
        raise AgentDefinitionError(f"Failed to parse frontmatter: {exc}") from exc

    trigger = fm.get("trigger", "manual")
    if trigger not in _VALID_TRIGGERS:
        raise AgentDefinitionError(
            f"Invalid trigger '{trigger}'. Must be one of: {', '.join(sorted(_VALID_TRIGGERS))}."
        )

    scope_raw = fm.get("scope", [])
    scope: list[str] = [scope_raw] if isinstance(scope_raw, str) else list(scope_raw)

    ref = fm.get("ref", "")
    schedule = fm.get("schedule", "")
    timezone = fm.get("timezone", "")
    model = fm.get("model", "")

    if trigger == "schedule" and not schedule:
        raise AgentDefinitionError(
            "trigger: schedule requires a 'schedule' field (cron expression)."
        )

    # Parse markdown body for name, description, skills, and fallback model
    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    name = ""

    for line in body.splitlines():
        if line.startswith("# Agent:"):
            name = line[len("# Agent:"):].strip()
        elif line.startswith("## "):
            current_section = line[3:].strip()
            sections.setdefault(current_section, [])
        elif current_section is not None:
            sections[current_section].append(line)

    def _section_text(key: str) -> str:
        return "\n".join(sections.get(key, [])).strip()

    description = _section_text("Description")
    if not model:
        model = _section_text("Model")

    skills = [
        line[2:].strip()
        for line in sections.get("Skills", [])
        if line.startswith("- ")
    ]

    if not ref and not name:
        raise AgentDefinitionError(
            "agent.md must have a '# Agent: {name}' heading, "
            "or a 'ref' frontmatter field for pointer agents."
        )

    return AgentDefinition(
        name=name,
        description=description,
        skills=skills,
        trigger=trigger,
        scope=scope,
        ref=ref,
        schedule=schedule,
        timezone=timezone,
        model=model,
    )
