"""Base agent infrastructure for AgentScribe."""

from __future__ import annotations

import dataclasses
import json
import os
import re
from typing import TYPE_CHECKING

from strands import Agent, tool
from strands.models.anthropic import AnthropicModel

if TYPE_CHECKING:
    import mypy_boto3_s3

DEFAULT_MODEL = "claude-opus-4-6"

# ── S3 path helpers ───────────────────────────────────────────────────────────

AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def agents_prefix(site: str, page_path: str = "") -> str:
    """Return the S3 prefix for the .agents directory of a page.

    page_path is relative to the site root (e.g. "" for root page,
    "child-page" for a child page).
    """
    if page_path:
        return f"{site}/{page_path}/.agents"
    return f"{site}/.agents"


def skills_prefix() -> str:
    """Return the S3 prefix for the shared .skills directory (bucket root)."""
    return ".skills"


# ── S3Tools (class-based tools) ───────────────────────────────────────────────


class S3Tools:
    """Strands class-based tools for reading and writing AgentScribe S3 objects."""

    def __init__(self, s3: "mypy_boto3_s3.S3Client", bucket: str) -> None:
        self.s3 = s3
        self.bucket = bucket

    # ── content helpers ───────────────────────────────────────────────────────

    def read_text(self, key: str) -> str:
        obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read().decode("utf-8")

    def write_text(self, key: str, text: str, content_type: str = "text/markdown; charset=utf-8") -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType=content_type,
        )

    def list_prefixes(self, prefix: str) -> list[str]:
        resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix + "/", Delimiter="/")
        return [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]

    # ── strands @tool methods ─────────────────────────────────────────────────

    @tool
    def get_content(self, site: str, page_path: str = "") -> str:
        """Retrieve the content.md for a wiki page from S3.

        Args:
            site: The site name (top-level S3 prefix).
            page_path: Relative path of the page within the site.
                       Empty string for the root page.
        """
        key = f"{site}/{page_path}/content.md" if page_path else f"{site}/content.md"
        try:
            return self.read_text(key)
        except Exception:
            return ""

    @tool
    def put_content(self, site: str, content: str, page_path: str = "") -> str:
        """Save updated content.md for a wiki page back to S3.

        Args:
            site: The site name.
            content: Full updated markdown content.
            page_path: Relative page path; empty for root.
        """
        key = f"{site}/{page_path}/content.md" if page_path else f"{site}/content.md"
        self.write_text(key, content)
        return f"Saved content to {key}"

    @tool
    def list_skills(self) -> str:
        """List all available skills and summarise what each one does.

        Reads the skill.md for every skill in the shared .skills directory
        so the caller can understand the available capabilities.
        """
        prefix = skills_prefix()
        names = self.list_prefixes(prefix)
        if not names:
            return "No skills are currently configured on this server."
        parts = []
        for name in names:
            key = f"{prefix}/{name}/skill.md"
            try:
                description = self.read_text(key)
                parts.append(f"### {name}\n\n{description.strip()}")
            except Exception:
                parts.append(f"### {name}\n\n(No description available.)")
        return "## Available Skills\n\n" + "\n\n---\n\n".join(parts)

    @tool
    def get_skill(self, skill_name: str) -> str:
        """Retrieve the skill.md definition for a named skill.

        Args:
            skill_name: Name of the skill to retrieve.
        """
        key = f"{skills_prefix()}/{skill_name}/skill.md"
        return self.read_text(key)

    @tool
    def get_skill_mcp_config(self, skill_name: str) -> str:
        """Retrieve the mcp.json for a named skill.

        Args:
            skill_name: Skill name.
        """
        key = f"{skills_prefix()}/{skill_name}/mcp.json"
        return self.read_text(key)

    def is_remote_skill(self, skill_name: str) -> bool:
        """Return True if the skill uses remote HTTP transport (has a 'url' key in mcp.json)."""
        key = f"{skills_prefix()}/{skill_name}/mcp.json"
        try:
            raw = self.read_text(key)
            config = json.loads(raw)
            return any("url" in srv for srv in config.get("mcpServers", {}).values())
        except Exception:
            return False

    @tool
    def get_skill_required_vars(self, skill_name: str) -> str:
        """Return the credential requirements for a skill's mcp.json.

        For remote OAuth skills, explains that OAuth authentication is required.
        For stdio skills, lists the ${VAR_NAME} environment variable placeholders.
        Call this after selecting a skill to know what credentials the user must provide.

        Args:
            skill_name: Name of the skill to inspect.
        """
        key = f"{skills_prefix()}/{skill_name}/mcp.json"
        try:
            raw = self.read_text(key)
        except Exception:
            return f"Skill '{skill_name}' has no mcp.json — no credentials required."
        try:
            config = json.loads(raw)
            if any("url" in srv for srv in config.get("mcpServers", {}).values()):
                return (
                    f"Skill '{skill_name}' uses remote OAuth authentication — no API keys required. "
                    f"Users must authenticate via OAuth in the Credentials panel before this skill can be used."
                )
        except Exception:
            pass
        vars_found = list(dict.fromkeys(re.findall(r"\$\{([A-Z0-9_]+)\}", raw)))
        if not vars_found:
            return f"Skill '{skill_name}' requires no API keys or environment variables."
        return f"Skill '{skill_name}' requires: {', '.join(vars_found)}"

    @tool
    def list_agents(self, site: str, page_path: str = "") -> str:
        """List agents defined for a wiki page.

        Args:
            site: The site name.
            page_path: Relative page path; empty for root.
        """
        prefix = agents_prefix(site, page_path)
        names = self.list_prefixes(prefix)
        if not names:
            return "No agents defined for this page."
        return "Available agents: " + ", ".join(names)

    @tool
    def put_agent(
        self,
        site: str,
        agent_name: str,
        description: str,
        skills: list[str],
        page_path: str = "",
        schedule: str = "",
        timezone: str = "",
    ) -> str:
        """Create or overwrite an agent.md file in S3.

        Args:
            site: The site name.
            agent_name: Name of the agent (lowercase, alphanumeric/hyphen/underscore).
            description: Agent purpose / system prompt.
            skills: List of skill names the agent should use.
            page_path: Relative page path; empty for root.
            schedule: Optional cron expression for scheduled execution (e.g. "0 * * * *").
            timezone: Optional timezone for the schedule (e.g. "America/New_York"). Defaults to UTC.
        """
        if not AGENT_NAME_RE.match(agent_name):
            return f"Error: invalid agent name {agent_name!r}. Use lowercase letters, digits, hyphens, underscores."
        skills_list = "\n".join(f"- {s}" for s in skills)
        optional_sections = ""
        if schedule:
            optional_sections += f"## Schedule\n\n{schedule}\n\n"
        if timezone:
            optional_sections += f"## Timezone\n\n{timezone}\n\n"
        content = (
            f"# Agent: {agent_name}\n\n"
            f"## Description\n\n{description}\n\n"
            f"{optional_sections}"
            f"## Skills\n\n{skills_list}\n"
        )
        prefix = agents_prefix(site, page_path)
        key = f"{prefix}/{agent_name}/agent.md"
        self.write_text(key, content)
        return f"Agent '{agent_name}' created. View/edit at #/.agents/{agent_name}"

    @tool
    def create_page(self, site: str, page_path: str) -> str:
        """Create a new wiki page in S3 with an empty content.md and .agents directory marker.

        Args:
            site: The site name.
            page_path: Path of the new page relative to site root.
        """
        if not re.match(r"^[a-z0-9][a-z0-9_/-]*$", page_path):
            return f"Error: invalid page path {page_path!r}. Use lowercase alphanumerics, hyphens, underscores, slashes."
        content_key = f"{site}/{page_path}/content.md"
        self.write_text(content_key, f"# {page_path.split('/')[-1].replace('-', ' ').title()}\n\n")
        # Write a .agents directory marker (S3 doesn't need it, but keeps structure clear)
        self.write_text(f"{site}/{page_path}/.agents/.keep", "")
        return f"Page '{page_path}' created at {content_key}"


# ── AgentDefinition + parser ──────────────────────────────────────────────────


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

        ## Schedule        ← optional

        {cron}

        ## Timezone        ← optional

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


# ── BaseAgent ─────────────────────────────────────────────────────────────────


class BaseAgent(Agent):
    """All AgentScribe agents inherit from this.

    Subclasses must define SYSTEM_PROMPT as a class variable.
    String-template placeholders (``{var}``) in SYSTEM_PROMPT are filled
    in by passing kwargs to the constructor.
    """

    SYSTEM_PROMPT: str = ""

    def __init__(
        self,
        tools: list,
        model_id: str = DEFAULT_MODEL,
        **prompt_vars,
    ) -> None:
        model = AnthropicModel(
            model_id=model_id,
            max_tokens=4096,
        )
        formatted_prompt = self.SYSTEM_PROMPT.format(**prompt_vars) if prompt_vars else self.SYSTEM_PROMPT
        super().__init__(
            system_prompt=formatted_prompt,
            model=model,
            tools=tools,
            callback_handler=None,
            load_tools_from_directory=False,
        )
