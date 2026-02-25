"""Standalone AgentDefinition dataclass and agent.md parser."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class AgentDefinition:
    name: str
    description: str
    skills: list[str]
    schedule: str = ""
    timezone: str = ""


def parse_agent_md(text: str) -> AgentDefinition:
    """Parse an agent.md file into an AgentDefinition.

    Expects the following structure (Schedule and Timezone sections are optional):

        # Agent: {name}

        ## Description

        {description}

        ## Schedule        <- optional

        {cron}

        ## Timezone        <- optional

        {tz}

        ## Skills

        - skill-a
        - skill-b
    """
    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    name = ""

    for line in text.splitlines():
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
    schedule = _section_text("Schedule")
    timezone = _section_text("Timezone")
    skills = [
        line[2:].strip()
        for line in sections.get("Skills", [])
        if line.startswith("- ")
    ]

    return AgentDefinition(
        name=name,
        description=description,
        skills=skills,
        schedule=schedule,
        timezone=timezone,
    )
