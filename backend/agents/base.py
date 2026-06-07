"""Base agent infrastructure for YoloScribe."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

from strands import Agent, ModelRetryStrategy, tool

from .models import DEFAULT_MODEL_KEY, build_strands_model

from yoloscribe_io import (
    AgentDefinition,
    AgentMarkdownFile,
    S3StorageBackend,
    SkillDefinition,
    SkillMarkdownFile,
    StorageBackend,
    WikiPageMarkdownFile,
    build_agent_md,
    list_tools as _list_tools_lib,
    load_tool_config,
    parse_agent_md,
)

# ── Constants ─────────────────────────────────────────────────────────────────

AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_MAX_DESCRIPTION_CHARS = 4_096
_MAX_RUNNER_PROMPT_CHARS = 2_048

# ── Prompt-injection detector ─────────────────────────────────────────────────

_INJECTION_RE = re.compile(
    r"ignore\s+(all\s+)?(previous|prior)\s+instructions?"
    r"|disregard\s+(all\s+)?(previous|prior)\s+instructions?"
    r"|forget\s+(everything|all\s+(previous|prior|above))"
    r"|your\s+new\s+(role|instructions?|directive|task|goal)"
    r"|</?system>"
    r"|\[/?INST\]"
    r"|<<SYS>>"
    r"|you\s+are\s+now\s+(?:a|an|the)\s+\w",
    re.IGNORECASE,
)


def _check_injection(text: str, field_name: str) -> str | None:
    """Return an error string if *text* matches a known injection pattern, else None."""
    if _INJECTION_RE.search(text):
        return (
            f"Error: {field_name} contains a disallowed pattern. "
            f"Remove prompt-injection language and try again."
        )
    return None


# ── Path helpers (used by chat.py runner tool) ────────────────────────────────

def agents_prefix(site: str, page_path: str = "") -> str:
    """Return the S3 prefix for the .agents directory of a page."""
    if page_path:
        return f"{site}/{page_path}/.agents"
    return f"{site}/.agents"


def tools_prefix() -> str:
    """Return the S3 prefix for the shared .tools directory (bucket root)."""
    return ".tools"


def skills_prefix(site: str) -> str:
    """Return the S3 prefix for the site's .skills directory."""
    return f"{site}/.skills"


# ── WikiPageTools ─────────────────────────────────────────────────────────────


class WikiPageTools:
    """Strands tools for reading and writing a specific wiki page.

    site and page_path are bound at construction via the WikiPageMarkdownFile
    argument; the LLM tools take no site/path arguments so they cannot be
    coerced into operating on arbitrary pages.
    """

    def __init__(self, wiki: WikiPageMarkdownFile, user_id: str = "") -> None:
        self._wiki = wiki
        self._user_id = user_id
        self._etag: str | None = None
        self.write_conflict: bool = False

    @property
    def site(self) -> str:
        return self._wiki.site

    @property
    def page_path(self) -> str:
        return self._wiki.page_path

    @tool
    def read_page(self) -> str:
        """Retrieve the current content of this wiki page."""
        content, etag = self._wiki.read_with_etag()
        self._etag = etag or None
        return content or ""

    @tool
    def write_page(self, content: str) -> str:
        """Save updated Markdown content to this wiki page.

        Args:
            content: The complete updated Markdown content to write.
        """
        saved = self._wiki.write_conditional(content, self._etag, user_id=self._user_id)
        if not saved:
            self.write_conflict = True
            return (
                "Write conflict: the page was modified by another writer between "
                "your read and write. Do not retry — this will be handled automatically."
            )
        return "Page saved."


# ── SiteTools ─────────────────────────────────────────────────────────────────


class SiteTools:
    """Strands tools for site-level operations: skills, agents, and pages.

    site is bound at construction. All tools operate exclusively within this site.
    """

    def __init__(self, site: str, storage: StorageBackend, user_id: str = "") -> None:
        self._site = site
        self._storage = storage
        self._user_id = user_id

    @property
    def site(self) -> str:
        return self._site

    # ── Skills ────────────────────────────────────────────────────────────────

    @tool
    def list_skills(self) -> str:
        """List all skills available in this site and summarise what each one does.

        Reads the SKILL.md for every skill and returns a formatted summary.
        """
        prefix = f"{self._site}/.skills/"
        names = sorted(set(
            k[len(prefix):].split("/")[0]
            for k in self._storage.list(prefix)
            if k.endswith("/SKILL.md")
        ))
        if not names:
            return "No skills are currently defined for this site."
        parts = []
        for name in names:
            skill = SkillMarkdownFile(self._site, name, self._storage)
            defn = skill.definition
            tools_str = ", ".join(defn.tools) if defn.tools else "none"
            parts.append(
                f"### {name}\n\n{defn.description or '(No description)'}\n\nTools: {tools_str}"
            )
        return "## Available Skills\n\n" + "\n\n---\n\n".join(parts)

    def _get_skill_defn(self, skill_name: str) -> SkillDefinition | None:
        skill = SkillMarkdownFile(self._site, skill_name, self._storage)
        raw = skill.raw_content
        return skill.definition if raw else None

    @tool
    def get_skill(self, skill_name: str) -> str:
        """Read a specific skill's full definition including instructions.

        Call this to understand what a skill does and what parameters the agent
        description must define before using it.

        Args:
            skill_name: Name of the skill to retrieve.
        """
        skill = SkillMarkdownFile(self._site, skill_name, self._storage)
        raw = skill.raw_content
        if not raw:
            return f"Skill '{skill_name}' not found."
        defn = skill.definition
        tools_str = ", ".join(defn.tools) if defn.tools else "none"
        return f"### {skill_name}\n\n{defn.description}\n\nTools: {tools_str}\n\n{defn.instructions}"

    @tool
    def get_skill_required_vars(self, skill_name: str) -> str:
        """Return credential variable names required by all tools in a skill.

        Reads the skill's tool list from its frontmatter, then collects the
        ${VAR_NAME} placeholders from each tool's mcp.json. Returns a plain
        text summary suitable for showing to the user.

        Args:
            skill_name: Name of the skill to inspect.
        """
        defn = self._get_skill_defn(skill_name)
        if defn is None:
            return f"Skill '{skill_name}' not found."
        if not defn.tools:
            return f"Skill '{skill_name}' uses no tools, so no credentials are required."
        all_vars: list[str] = []
        for tool_name in defn.tools:
            raw = self._storage.read(f".tools/{tool_name}/mcp.json") or ""
            vars_ = list(dict.fromkeys(re.findall(r"\$\{([A-Z0-9_]+)\}", raw)))
            all_vars.extend(v for v in vars_ if v not in all_vars)
        if not all_vars:
            return f"Skill '{skill_name}' requires no credentials."
        return (
            f"Skill '{skill_name}' requires the following credentials: "
            + ", ".join(all_vars)
        )

    def get_skill_tools(self, skill_name: str) -> list[str]:
        """Return the tool names declared by a skill (not an LLM tool)."""
        defn = self._get_skill_defn(skill_name)
        return defn.tools if defn else []

    # ── Agents ────────────────────────────────────────────────────────────────

    @tool
    def list_agents(self, page_path: str = "") -> str:
        """List agents defined for a wiki page.

        Args:
            page_path: Relative page path; empty for the root page.
        """
        prefix = (
            f"{self._site}/{page_path}/.agents/"
            if page_path
            else f"{self._site}/.agents/"
        )
        names = sorted(set(
            k[len(prefix):].split("/")[0]
            for k in self._storage.list(prefix)
            if k.endswith("/agent.md")
        ))
        if not names:
            return "No agents defined for this page."
        return "Available agents: " + ", ".join(names)

    @tool
    def list_all_agents(self) -> str:
        """List every agent across the entire site.

        Scans all .agents/ directories site-wide and returns each agent's name,
        page location, and trigger type. Useful before creating a new agent to
        check whether one already exists for the intended purpose.
        """
        found: list[str] = []
        site_prefix = f"{self._site}/"
        for key in self._storage.list(site_prefix):
            if not key.endswith("/agent.md") or "/.agents/" not in key:
                continue
            rel = key[len(site_prefix):]
            parts = rel.split("/")
            try:
                agents_idx = parts.index(".agents")
            except ValueError:
                continue
            agent_name = parts[agents_idx + 1]
            page_loc = "/".join(parts[:agents_idx]) if agents_idx > 0 else "(root)"
            raw = self._storage.read(key) or ""
            trigger_match = re.search(r"^trigger:\s*(\S+)", raw, re.MULTILINE)
            trigger = trigger_match.group(1) if trigger_match else "manual"
            found.append(f"- **{agent_name}** at `{page_loc}` (trigger: {trigger})")
        if not found:
            return "No agents found on this site."
        return "All agents on this site:\n\n" + "\n".join(found)

    @tool
    def create_agent(
        self,
        agent_name: str,
        description: str,
        skills: list[str],
        agent_type: str = "",
        page_path: str = "",
        trigger: str = "manual",
        schedule: str = "",
        timezone: str = "",
        model: str = "",
        confirm_before_write: bool = False,
        events: list[str] | None = None,
        overwrite: bool = False,
    ) -> str:
        """Create a new agent.md file. Set overwrite=True to replace an existing agent.

        Args:
            agent_name: Name of the agent (lowercase, alphanumeric/hyphen/underscore).
            description: Agent purpose / system prompt.
            skills: List of skill names the agent should use.
            agent_type: Agent class — "page", "ingest", or "notification". Empty string
                        lets the runner infer the type from trigger/path heuristics.
            page_path: Relative page path; empty for root. Overridden automatically
                       for ingest (→ .user/ingest) and notification (→ site root) types.
            trigger: When the agent runs — "manual", "schedule", "on_write", or "on_notify".
            schedule: Cron expression — required when trigger is "schedule".
            timezone: Timezone for the schedule (e.g. "America/New_York"). Defaults to UTC.
            model: Optional model key from the registry (e.g. "sonnet", "bedrock-opus").
                   Leave blank to use the server default.
            confirm_before_write: When true the agent writes proposed changes to
                                  .proposed.content.md instead of content.md directly.
            events: Event types to watch — required when trigger is "on_notify".
                    E.g. ["page_shared", "access_requested"].
            overwrite: If False (default) and an agent with this name already exists,
                       the call is rejected. Pass True to intentionally replace it.
        """
        if not AGENT_NAME_RE.match(agent_name):
            return (
                f"Error: invalid agent name {agent_name!r}. "
                f"Use lowercase letters, digits, hyphens, underscores."
            )
        if len(description) > _MAX_DESCRIPTION_CHARS:
            return (
                f"Error: description is too long ({len(description)} chars). "
                f"Maximum is {_MAX_DESCRIPTION_CHARS} characters."
            )
        if err := _check_injection(description, "description"):
            return err

        valid_triggers = {"manual", "schedule", "on_write", "on_notify"}
        if trigger not in valid_triggers:
            return (
                f"Error: invalid trigger '{trigger}'. "
                f"Use one of: manual, schedule, on_write, on_notify."
            )
        if trigger == "schedule" and not schedule:
            return "Error: trigger 'schedule' requires a 'schedule' cron expression."
        if trigger == "on_notify" and not events:
            return (
                "Error: trigger 'on_notify' requires an 'events' list. "
                "Specify which notification event types this agent should handle "
                "(e.g. [\"page_shared\", \"access_requested\"])."
            )

        valid_types = {"", "page", "ingest", "notification"}
        if agent_type not in valid_types:
            return (
                f"Error: invalid agent_type '{agent_type}'. "
                f"Use one of: page, ingest, notification (or leave empty)."
            )

        # Enforce canonical paths per type.
        if agent_type == "ingest":
            page_path = ".user/ingest"
        elif agent_type == "notification" or trigger == "on_notify":
            page_path = ""

        agent_file = AgentMarkdownFile(self._site, page_path, agent_name, self._storage)
        if not overwrite and self._storage.read(agent_file.key) is not None:
            return (
                f"Error: agent '{agent_name}' already exists. "
                f"Pass overwrite=True to replace it, or choose a different name."
            )

        defn = AgentDefinition(
            name=agent_name,
            description=description,
            skills=skills,
            trigger=trigger,
            schedule=schedule,
            timezone=timezone,
            model=model,
            confirm_before_write=confirm_before_write,
            events=list(events) if events else [],
            type=agent_type,
        )
        if overwrite:
            agent_file.save(defn)
        else:
            agent_file.create(defn)

        location = f"#/{page_path}/.agents/{agent_name}" if page_path else f"#/.agents/{agent_name}"
        return f"Agent '{agent_name}' created. View/edit at {location}"

    # ── Skills (write) ────────────────────────────────────────────────────────

    @tool
    def create_skill(
        self,
        name: str,
        description: str,
        tools_list: list[str],
        body: str,
        overwrite: bool = False,
    ) -> str:
        """Write a new SKILL.md to the site's .skills directory.

        Only call this after gathering all required information from the user
        conversationally. See the system prompt for the required steps.

        Args:
            name: Skill name (lowercase, alphanumeric/hyphen/underscore).
            description: One-line description shown in the UI.
            tools_list: List of tool names (from the shared .tools directory) the skill uses.
            body: The skill's instruction body — the system prompt text injected when active.
            overwrite: If False (default) and a skill with this name already exists,
                       the call is rejected. Pass True to intentionally replace it.
        """
        if not AGENT_NAME_RE.match(name):
            return (
                f"Error: invalid skill name {name!r}. "
                f"Use lowercase letters, digits, hyphens, underscores."
            )
        skill_file = SkillMarkdownFile(self._site, name, self._storage)
        if not overwrite and self._storage.read(skill_file.key) is not None:
            return (
                f"Skill '{name}' already exists. "
                f"Pass overwrite=True to replace it, or choose a different name."
            )
        defn = SkillDefinition(
            name=name, description=description, tools=tools_list, instructions=body
        )
        if overwrite:
            skill_file.save(defn)
        else:
            skill_file.create(defn)
        return f"Skill '{name}' created. View/edit at #/.skills/{name}"

    # ── Pages ─────────────────────────────────────────────────────────────────

    @tool
    def create_page(self, page_path: str, content: str = "") -> str:
        """Create a new wiki page with a content.md and .agents directory marker.

        Args:
            page_path: Path of the new page relative to site root.
            content: Optional markdown content. If omitted, a default welcome page is written.
                     Supply this when the user has specified what they want on the page.
        """
        if not re.match(r"^[a-z0-9][a-z0-9_/-]*$", page_path):
            return (
                f"Error: invalid page path {page_path!r}. "
                f"Use lowercase alphanumerics, hyphens, underscores, slashes."
            )
        if not content:
            title = page_path.split("/")[-1].replace("-", " ").title()
            content = (
                f"# {title}\n\n"
                f"This is a new wiki page. Edit this content using the editor,\n"
                f"or ask the AI assistant in the Chat panel to help you write and organise your notes.\n\n"
                f"## Getting Started\n\n"
                f"- Click **Edit** to enter edit mode\n"
                f"- Use the **Chat** panel to ask the AI to help you write content\n"
                f"- Navigate to sub-pages by clicking links\n"
            )
        wiki = WikiPageMarkdownFile(self._site, page_path, self._storage)
        wiki.create(content, user_id=self._user_id)
        self._storage.write(f"{self._site}/{page_path}/.agents/.keep", "")
        return f"Page '{page_path}' created at {wiki.key}"

    # ── Tool introspection (used by orchestrator, not LLM tools) ─────────────

    def list_tool_names(self) -> list[str]:
        """Return names of all tools in the shared .tools directory."""
        return _list_tools_lib(self._storage)

    def is_remote_tool(self, tool_name: str) -> bool:
        """Return True if the tool uses OAuth (remote HTTP transport)."""
        cfg = load_tool_config(tool_name, self._storage)
        return cfg.requires_oauth if cfg else False


# ── BaseAgent ─────────────────────────────────────────────────────────────────


class BaseAgent(Agent):
    """All YoloScribe agents inherit from this.

    Subclasses must define SYSTEM_PROMPT as a class variable.
    String-template placeholders (``{var}``) in SYSTEM_PROMPT are filled
    in by passing kwargs to the constructor.
    """

    SYSTEM_PROMPT: str = ""

    def __init__(
        self,
        tools: list,
        model_key: str = DEFAULT_MODEL_KEY,
        **prompt_vars,
    ) -> None:
        model = build_strands_model(model_key)
        formatted_prompt = self.SYSTEM_PROMPT.format(**prompt_vars) if prompt_vars else self.SYSTEM_PROMPT
        super().__init__(
            system_prompt=formatted_prompt,
            model=model,
            tools=tools,
            callback_handler=None,
            load_tools_from_directory=False,
            retry_strategy=ModelRetryStrategy(
                max_attempts=8,
                initial_delay=10,
                max_delay=120,
            ),
        )
