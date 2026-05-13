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
    schedule: str = ""
    timezone: str = ""
    model: str = ""
    confirm_before_write: bool = False


_VALID_TRIGGERS = frozenset({"manual", "schedule", "on_write", "on_notify"})


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
        trigger:   manual | schedule | on_write | on_notify  (default: manual)
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

    schedule = fm.get("schedule", "")
    timezone = fm.get("timezone", "")
    model = fm.get("model", "")
    confirm_before_write = str(fm.get("confirm_before_write", "")).lower() in ("true", "yes", "1")

    # name and skills may come from frontmatter (new format) or body sections (old format)
    name = fm.get("name", "")
    fm_skills_raw = fm.get("skills", [])
    fm_skills: list[str] = [fm_skills_raw] if isinstance(fm_skills_raw, str) else list(fm_skills_raw)

    if trigger == "schedule" and not schedule:
        raise AgentDefinitionError(
            "trigger: schedule requires a 'schedule' field (cron expression)."
        )

    # Parse body: supports both new format (free-form text) and old format (## sections)
    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    free_body_lines: list[str] = []

    for line in body.splitlines():
        if line.startswith("# Agent:"):
            if not name:
                name = line[len("# Agent:"):].strip()
            # skip the heading line — not part of the description
        elif line.startswith("## "):
            current_section = line[3:].strip()
            sections.setdefault(current_section, [])
        elif current_section is not None:
            sections[current_section].append(line)
        else:
            free_body_lines.append(line)

    def _section_text(key: str) -> str:
        return "\n".join(sections.get(key, [])).strip()

    # Description: ## Description section (old format) or free-form body (new format)
    if "Description" in sections:
        description = _section_text("Description")
    else:
        description = "\n".join(free_body_lines).strip()

    # Skills: frontmatter takes priority over ## Skills body section
    if fm_skills:
        skills = fm_skills
    else:
        skills = [
            line[2:].strip()
            for line in sections.get("Skills", [])
            if line.startswith("- ")
        ]

    # Model: frontmatter takes priority over ## Model body section
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
    )
