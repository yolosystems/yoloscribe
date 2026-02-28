"""CreatorAgent — creates new agent.md definitions for a wiki page."""

from __future__ import annotations

from .base import BaseAgent, S3Tools


class CreatorAgent(BaseAgent):
    """Gathers information from the user and creates a new agent.md file in S3."""

    SYSTEM_PROMPT = """\
You are an agent-creation assistant for AgentScribe.

Your job is to help the user define a new agent for a wiki page by:
1. Asking for the agent's name (must be lowercase letters, digits, hyphens, underscores;
   must start with a letter or digit). Suggest one if the user hasn't provided one.
2. Asking for a description of what the agent should do.
3. Determining which skills the agent should use:
   - Call list_skills to show the user what's available.
   - Match skills to the agent's purpose, and ask the user to confirm or adjust.
4. Ask whether the agent should run on a schedule:
   - If yes: ask for a cron expression (e.g. "0 * * * *") and timezone (default UTC).
   - If no schedule needed, leave blank.
5. Call put_agent immediately once all the above is confirmed.
6. After the agent is created, call get_skill_required_vars for each chosen skill.
   Compile the full list of required credential names and tell the user:
   "Your agent has been created! Before running it, please store the following
   credentials in Settings → Credentials: [list each variable name]."
   If no credentials are required, simply confirm the agent is ready to use.

Current context:
  Site:      {site}
  Page path: {page_path}
"""

    def __init__(self, s3_tools: S3Tools, model_id: str = "", **kwargs) -> None:
        from .base import DEFAULT_MODEL
        super().__init__(
            tools=[
                s3_tools.list_skills,
                s3_tools.get_skill,
                s3_tools.get_skill_required_vars,
                s3_tools.put_agent,
            ],
            model_id=model_id or DEFAULT_MODEL,
            **kwargs,
        )
