from .archive import router as archive_router
from .assets import router as assets_router
from .ingest import router as ingest_router
from .chat import router as chat_router
from .content import router as content_router
from .health import router as health_router
from .mcp_oauth import router as mcp_oauth_router
from .message import router as message_router
from .messaging import router as messaging_router
from .oauth import router as oauth_router
from .obsidian import router as obsidian_router
from .pages import router as pages_router
from .settings import router as settings_router
from .site import router as site_router
from .token_budget import router as token_budget_router
from .tokens import router as tokens_router
from .tools import router as tools_router
from .outbound_webhooks import router as outbound_webhooks_router
from .search import router as search_router
from .versions import router as versions_router
from .webhooks import router as webhooks_router

__all__ = [
    "assets_router",
    "ingest_router",
    "chat_router",
    "content_router",
    "health_router",
    "mcp_oauth_router",
    "message_router",
    "messaging_router",
    "oauth_router",
    "obsidian_router",
    "pages_router",
    "settings_router",
    "site_router",
    "token_budget_router",
    "tokens_router",
    "tools_router",
    "archive_router",
    "outbound_webhooks_router",
    "search_router",
    "versions_router",
    "webhooks_router",
]
