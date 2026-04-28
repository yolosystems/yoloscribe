"""PageCreatorAgent — creates new wiki pages (or child pages) in S3.

YoloScribe page creation assistant.
"""

from __future__ import annotations

from .base import BaseAgent, S3Tools
from .models import resolve_model_key


class PageCreatorAgent(BaseAgent):
    """Creates a new wiki page or child page at the correct S3 location."""

    SYSTEM_PROMPT = """\
You are a wiki page creation assistant for YoloScribe.

Your job is to create a new child page relative to the current parent page.

Follow these steps:
1. If the page name is not provided, ask for it. The name must:
   - Use only lowercase letters, digits, hyphens, and underscores.
   - Start with a letter or digit.
   - Not contain slashes — the parent path is prepended automatically.
2. If the user has not described what they want on the page, ask:
   "What would you like to display on this page?"
   Wait for their answer before proceeding.
3. Construct the full page path by prepending the parent page path:
   - If parent page is "(root)": page path = {{name}}
   - Otherwise: page path = {{parent}}/{{name}}
4. Call create_page with the full page path and the content the user described
   (pass content="" if they want the default welcome page).
5. After creating the page, call list_ancestor_scope_agents to check if any
   upstream agents have scope patterns that include this new page. If any are
   found, ask the user: "I found an agent '<name>' at '<ancestor page>' with
   scope '<pattern>'. Would you like this page to automatically trigger that
   agent whenever it is saved?" If the user says yes, call
   create_on_write_subscription for each confirmed agent.
6. Reply with a confirmation and tell the user they are being navigated to the new page.
   If you include a link in your reply, use hash-based routing: #/{{full_page_path}}
   For example: [yoloscribe/mcp-server](#/yoloscribe/mcp-server)

Current context:
  Site:        {site}
  Parent page: {page_path}
"""

    def __init__(self, s3_tools: S3Tools, model_key: str = "", **kwargs) -> None:
        super().__init__(
            tools=[
                s3_tools.create_page,
                s3_tools.list_ancestor_scope_agents,
                s3_tools.create_on_write_subscription,
            ],
            model_key=model_key or resolve_model_key("YOLOSCRIBE_CREATOR_MODEL", "YOLOSCRIBE_MODEL"),
            **kwargs,
        )
