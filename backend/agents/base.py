"""Base agent infrastructure for YoloScribe."""

from __future__ import annotations

import dataclasses
import json
import os
import re
from typing import TYPE_CHECKING

from strands import Agent, ModelRetryStrategy, tool

from .models import DEFAULT_MODEL_KEY, build_strands_model

if TYPE_CHECKING:
    import mypy_boto3_s3

# ── S3 path helpers ───────────────────────────────────────────────────────────

AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# ── Prompt-injection detector ─────────────────────────────────────────────────

# Common trigger phrases used in prompt-injection attacks.  This is a best-effort
# deterministic filter; it runs before the LLM layer so it cannot be defeated by
# the LLM itself.  False positives are possible — the patterns are intentionally
# conservative (they match only well-known injection preambles, not generic prose).
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

_MAX_DESCRIPTION_CHARS = 4_096
_MAX_RUNNER_PROMPT_CHARS = 2_048


def _check_injection(text: str, field_name: str) -> str | None:
    """Return an error string if *text* matches a known injection pattern, else None."""
    if _INJECTION_RE.search(text):
        return (
            f"Error: {field_name} contains a disallowed pattern. "
            f"Remove prompt-injection language and try again."
        )
    return None


def agents_prefix(site: str, page_path: str = "") -> str:
    """Return the S3 prefix for the .agents directory of a page.

    page_path is relative to the site root (e.g. "" for root page,
    "child-page" for a child page).
    """
    if page_path:
        return f"{site}/{page_path}/.agents"
    return f"{site}/.agents"


def tools_prefix() -> str:
    """Return the S3 prefix for the shared .tools directory (bucket root)."""
    return ".tools"


def skills_prefix(site: str) -> str:
    """Return the S3 prefix for the per-site .skills directory."""
    return f"{site}/.skills"


# ── Frontmatter parser ────────────────────────────────────────────────────────


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML-style frontmatter from a markdown string.

    Returns (frontmatter_dict, body_text).  The frontmatter must be delimited
    by ``---`` lines at the very start of the file.  Only the simple types
    needed for SKILL.md are supported: string scalars and string lists.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_text = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")

    fm: dict = {}
    current_key: str | None = None
    for line in fm_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            # List item under the current key
            item = stripped[2:].strip().strip("\"'")
            if current_key is not None:
                if not isinstance(fm.get(current_key), list):
                    fm[current_key] = []
                fm[current_key].append(item)
        elif ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip().strip("\"'")
            current_key = key
            if value:
                fm[key] = value
            else:
                fm[key] = []

    return fm, body


# ── S3Tools (class-based tools) ───────────────────────────────────────────────


class S3Tools:
    """Strands class-based tools for reading and writing YoloScribe S3 objects."""

    def __init__(
        self,
        s3: "mypy_boto3_s3.S3Client",
        bucket: str,
        user_site: str | None = None,
        user_email: str | None = None,
    ) -> None:
        self.s3 = s3
        self.bucket = bucket
        self._user_site = user_site
        self._user_email = user_email
        # ETag captured on get_content; used as IfMatch on put_content.
        self._etag_cache: dict[str, str] = {}
        # Set True by put_content when a 412 PreconditionFailed is returned by
        # S3. The caller (content_writer tool in chat.py) checks this flag to
        # decide whether to retry the agent invocation.
        self.write_conflict: bool = False

    # ── Ownership / access checks ─────────────────────────────────────────────

    def _require_site_ownership(self, site: str) -> None:
        """Raise PermissionError if the authenticated user does not own `site`.

        If no user_site is set (unauthenticated / internal call), the check is
        skipped to preserve backwards compatibility during rollout.
        """
        if self._user_site is not None and site != self._user_site:
            raise PermissionError(
                f"Access denied: site '{site}' is not owned by the authenticated user"
            )

    def _require_read_access(self, site: str, page_path: str) -> None:
        """Raise PermissionError if the authenticated user cannot read the page.

        Ownership grants unconditional read access.  For non-owners, falls back
        to checking the page's settings.json visibility (public = allowed;
        shared = allowed if the user's email is in shared_with).
        """
        if self._user_site is None:
            return  # No auth context — internal/unauthenticated call, skip check
        if site == self._user_site:
            return  # Owner always has read access

        # Not the owner — inspect visibility settings
        settings_key = (
            f"{site}/{page_path}/settings.json" if page_path else f"{site}/settings.json"
        )
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=settings_key)
            settings: dict = json.loads(obj["Body"].read())
        except Exception:
            raise PermissionError(
                f"Access denied: cannot read page '{page_path or '(root)'}' in site '{site}'"
            )

        visibility = settings.get("visibility", "private")
        if visibility == "public":
            return
        if visibility == "shared" and self._user_email:
            shared_with = settings.get("shared_with", [])
            if any(e.get("email") == self._user_email for e in shared_with):
                return

        raise PermissionError(
            f"Access denied: cannot read page '{page_path or '(root)'}' in site '{site}'"
        )

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
        self._require_read_access(site, page_path)
        key = f"{site}/{page_path}/content.md" if page_path else f"{site}/content.md"
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
            self._etag_cache[key] = obj["ETag"]
            return obj["Body"].read().decode("utf-8")
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
        self._require_site_ownership(site)
        key = f"{site}/{page_path}/content.md" if page_path else f"{site}/content.md"
        etag = self._etag_cache.get(key)
        kwargs: dict = {"IfMatch": etag} if etag else {}
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=content.encode("utf-8"),
                ContentType="text/markdown; charset=utf-8",
                **kwargs,
            )
        except self.s3.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in ("PreconditionFailed", "412"):
                self.write_conflict = True
                return (
                    "Write conflict: the page was modified by another writer between "
                    "your read and write. Do not retry — this will be handled automatically."
                )
            raise
        return f"Saved content to {key}"

    # ── Tools (bucket-root, admin-managed) ────────────────────────────────────

    def list_tool_names(self) -> list[str]:
        """Return names of all tools in the shared .tools directory."""
        return self.list_prefixes(tools_prefix())

    def get_tool_mcp_config(self, tool_name: str) -> dict:
        """Read .tools/{tool_name}/mcp.json and return as a dict."""
        key = f"{tools_prefix()}/{tool_name}/mcp.json"
        raw = self.read_text(key)
        return json.loads(raw)

    def is_remote_tool(self, tool_name: str) -> bool:
        """Return True if the tool's mcp.json uses remote HTTP transport."""
        try:
            config = self.get_tool_mcp_config(tool_name)
            return any("url" in srv for srv in config.get("mcpServers", {}).values())
        except Exception:
            return False

    def get_tool_required_vars(self, tool_name: str) -> list[str]:
        """Return the list of ${VAR_NAME} placeholders in a stdio tool's mcp.json."""
        key = f"{tools_prefix()}/{tool_name}/mcp.json"
        try:
            raw = self.read_text(key)
            return list(dict.fromkeys(re.findall(r"\$\{([A-Z0-9_]+)\}", raw)))
        except Exception:
            return []

    def get_tool_oauth_client(self, tool_name: str) -> dict | None:
        """Read .tools/{tool_name}/oauth_client.json, or None if absent."""
        key = f"{tools_prefix()}/{tool_name}/oauth_client.json"
        try:
            raw = self.read_text(key)
            return json.loads(raw)
        except Exception:
            return None

    # ── Skills (per-site, user-managed) ───────────────────────────────────────

    @tool
    def list_skills(self, site: str) -> str:
        """List all skills available in the user's site and summarise what each one does.

        Reads the SKILL.md for every skill in the site's .skills directory.

        Args:
            site: The site name.
        """
        self._require_site_ownership(site)
        prefix = skills_prefix(site)
        names = self.list_prefixes(prefix)
        if not names:
            return "No skills are currently defined for this site."
        parts = []
        for name in names:
            key = f"{prefix}/{name}/SKILL.md"
            try:
                text = self.read_text(key)
                fm, _ = _parse_frontmatter(text)
                description = fm.get("description", "(No description)")
                tool_list = fm.get("tools", [])
                tools_str = ", ".join(tool_list) if tool_list else "none"
                parts.append(f"### {name}\n\n{description}\n\nTools: {tools_str}")
            except Exception:
                parts.append(f"### {name}\n\n(No description available.)")
        return "## Available Skills\n\n" + "\n\n---\n\n".join(parts)

    def get_skill(self, site: str, skill_name: str) -> "SkillDefinition":
        """Read and parse {site}/.skills/{skill_name}/SKILL.md.

        Args:
            site: The site name.
            skill_name: Name of the skill to retrieve.
        """
        key = f"{skills_prefix(site)}/{skill_name}/SKILL.md"
        text = self.read_text(key)
        fm, body = _parse_frontmatter(text)
        tools_list = fm.get("tools", [])
        if isinstance(tools_list, str):
            tools_list = [tools_list]
        return SkillDefinition(
            name=skill_name,
            description=fm.get("description", ""),
            tools=tools_list,
            body=body,
        )

    def get_skill_tools(self, site: str, skill_name: str) -> list[str]:
        """Return the list of tool names referenced by a skill's frontmatter.

        Args:
            site: The site name.
            skill_name: Skill name.
        """
        try:
            return self.get_skill(site, skill_name).tools
        except Exception:
            return []

    @tool
    def get_skill_required_vars(self, site: str, skill_name: str) -> str:
        """Return the credential variable names required by all tools in a skill.

        Reads the skill's tool list from its frontmatter, then collects the
        ${VAR_NAME} placeholders from each tool's mcp.json.  Returns a plain
        text summary suitable for showing to the user.

        Args:
            site: The site name.
            skill_name: Name of the skill to inspect.
        """
        tool_names = self.get_skill_tools(site, skill_name)
        if not tool_names:
            return f"Skill '{skill_name}' uses no tools, so no credentials are required."
        all_vars: list[str] = []
        for tool_name in tool_names:
            vars_ = self.get_tool_required_vars(tool_name)
            all_vars.extend(v for v in vars_ if v not in all_vars)
        if not all_vars:
            return f"Skill '{skill_name}' requires no credentials."
        return (
            f"Skill '{skill_name}' requires the following credentials: "
            + ", ".join(all_vars)
        )

    def put_skill(self, site: str, skill_name: str, markdown: str, overwrite: bool = False) -> None:
        """Write a SKILL.md to {site}/.skills/{skill_name}/SKILL.md.

        Args:
            site: The site name.
            skill_name: Skill name (must match AGENT_NAME_RE).
            markdown: Full SKILL.md content including frontmatter.
            overwrite: If False (default) and a skill with this name already exists,
                       raise ValueError.  Pass True to intentionally replace it.
        """
        self._require_site_ownership(site)
        key = f"{skills_prefix(site)}/{skill_name}/SKILL.md"
        if not overwrite:
            resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=key, MaxKeys=1)
            if resp.get("KeyCount", 0) > 0:
                raise ValueError(
                    f"Skill '{skill_name}' already exists. "
                    f"Pass overwrite=True to replace it, or choose a different name."
                )
        self.write_text(key, markdown)

    @tool
    def list_agents(self, site: str, page_path: str = "") -> str:
        """List agents defined for a wiki page.

        Args:
            site: The site name.
            page_path: Relative page path; empty for root.
        """
        self._require_site_ownership(site)
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
        model: str = "",
        overwrite: bool = False,
    ) -> str:
        """Create a new agent.md file in S3.  Set overwrite=True to replace an existing agent.

        Args:
            site: The site name.
            agent_name: Name of the agent (lowercase, alphanumeric/hyphen/underscore).
            description: Agent purpose / system prompt.
            skills: List of skill names the agent should use.
            page_path: Relative page path; empty for root.
            schedule: Optional cron expression for scheduled execution (e.g. "0 * * * *").
            timezone: Optional timezone for the schedule (e.g. "America/New_York"). Defaults to UTC.
            model: Optional model key from the registry (e.g. "sonnet", "bedrock-opus").
                   Leave blank to use the server default.
            overwrite: If False (default) and an agent with this name already exists,
                       the call is rejected.  Pass True to intentionally replace it.
        """
        self._require_site_ownership(site)
        if not AGENT_NAME_RE.match(agent_name):
            return f"Error: invalid agent name {agent_name!r}. Use lowercase letters, digits, hyphens, underscores."
        if len(description) > _MAX_DESCRIPTION_CHARS:
            return (
                f"Error: description is too long ({len(description)} chars). "
                f"Maximum is {_MAX_DESCRIPTION_CHARS} characters."
            )
        if err := _check_injection(description, "description"):
            return err
        prefix = agents_prefix(site, page_path)
        key = f"{prefix}/{agent_name}/agent.md"
        if not overwrite:
            resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=key, MaxKeys=1)
            if resp.get("KeyCount", 0) > 0:
                return (
                    f"Error: agent '{agent_name}' already exists. "
                    f"Pass overwrite=True to replace it, or choose a different name."
                )
        skills_list = "\n".join(f"- {s}" for s in skills)
        optional_sections = ""
        if schedule:
            optional_sections += f"## Schedule\n\n{schedule}\n\n"
        if timezone:
            optional_sections += f"## Timezone\n\n{timezone}\n\n"
        if model:
            optional_sections += f"## Model\n\n{model}\n\n"
        content = (
            f"# Agent: {agent_name}\n\n"
            f"## Description\n\n{description}\n\n"
            f"{optional_sections}"
            f"## Skills\n\n{skills_list}\n"
        )
        self.write_text(key, content)
        return f"Agent '{agent_name}' created. View/edit at #/.agents/{agent_name}"

    @tool
    def create_page(self, site: str, page_path: str, content: str = "") -> str:
        """Create a new wiki page in S3 with a content.md and .agents directory marker.

        Args:
            site: The site name.
            page_path: Path of the new page relative to site root.
            content: Optional markdown content for the page. If omitted, a default
                     welcome page is written. Supply this when the user has specified
                     what they want the page to display.
        """
        self._require_site_ownership(site)
        if not re.match(r"^[a-z0-9][a-z0-9_/-]*$", page_path):
            return f"Error: invalid page path {page_path!r}. Use lowercase alphanumerics, hyphens, underscores, slashes."
        title = page_path.split("/")[-1].replace("-", " ").title()
        if not content:
            page_content = (
                f"# {title}\n\n"
                f"This is a new wiki page. Edit this content using the editor,\n"
                f"or ask the AI assistant in the Chat panel to help you write and organise your notes.\n\n"
                f"## Getting Started\n\n"
                f"- Click **Edit** to enter edit mode\n"
                f"- Use the **Chat** panel to ask the AI to help you write content\n"
                f"- Navigate to sub-pages by clicking links\n"
            )
        else:
            page_content = content
        content_key = f"{site}/{page_path}/content.md"
        self.write_text(content_key, page_content)
        # Write a .agents directory marker (S3 doesn't need it, but keeps structure clear)
        self.write_text(f"{site}/{page_path}/.agents/.keep", "")
        return f"Page '{page_path}' created at {content_key}"


# ── AgentDefinition + SkillDefinition + parsers ───────────────────────────────


@dataclasses.dataclass
class AgentDefinition:
    name: str
    description: str
    skills: list[str]
    schedule: str = ""
    timezone: str = ""
    model: str = ""


@dataclasses.dataclass
class SkillDefinition:
    name: str
    description: str   # from YAML frontmatter
    tools: list[str]   # tool names from .tools/ referenced in frontmatter
    body: str          # full SKILL.md body (injected as system prompt context at runtime)


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
    model = _section_text("Model")
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
        model=model,
    )


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
