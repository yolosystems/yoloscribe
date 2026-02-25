"""CreatorAgent — creates new agent.md definitions for a wiki page."""

from __future__ import annotations

from .base import BaseAgent, S3Tools, SecretsManagerTools


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
4. For each confirmed skill, call get_skill_required_vars to find out which API keys
   it needs. Then for each required variable:
   - Call check_user_secret_exists to see if a value is already stored.
   - If already stored: tell the user ("You already have a GITHUB_TOKEN stored.")
     and ask whether they want to keep the existing value or replace it.
   - If not stored: ask the user to supply the value.
   - Call put_user_secret to store any new or updated values.
   Never log or repeat secret values back to the user.
5. Ask whether the agent should run on a schedule:
   - If yes: ask for a cron expression (e.g. "0 * * * *") and timezone (default UTC).
   - If no schedule needed, leave blank.
6. Once all details are confirmed, call put_agent (with schedule/timezone if provided).
7. Reply with a confirmation and the URL to view/edit the agent: #/.agents/{{name}}

Current context:
  Site:      {site}
  Page path: {page_path}
"""

    def __init__(
        self,
        s3_tools: S3Tools,
        sm_tools: SecretsManagerTools,
        model_id: str = "",
        **kwargs,
    ) -> None:
        from .base import DEFAULT_MODEL
        super().__init__(
            tools=[
                s3_tools.list_skills,
                s3_tools.get_skill,
                s3_tools.get_skill_required_vars,
                s3_tools.put_agent,
                sm_tools.check_user_secret_exists,
                sm_tools.put_user_secret,
            ],
            model_id=model_id or DEFAULT_MODEL,
            **kwargs,
        )
