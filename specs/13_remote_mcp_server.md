# YoloScribe Remote MCP Server Specification

## 1. Overview & Goals

The YoloScribe Remote MCP (Model Context Protocol) Server provides a standardized interface for agentic coding tools (e.g., Claude Code, other AI coding assistants) to programmatically interact with YoloScribe's core capabilities over HTTP. Rather than relying on local stdio-based connections, this server exposes YoloScribe's functionality as a cloud-hosted remote service.

**Primary Goals:**
- Enable AI coding agents to autonomously create, read, update, and delete wiki content
- Support collaborative AI-assisted development workflows
- Provide powerful search and retrieval capabilities for context-aware agent decision-making
- Expose agent lifecycle management for multi-step agentic workflows
- Maintain security and access control through API authentication
- Operate as a stateless, horizontally-scalable HTTP service

**Target Consumers:**
- Claude Code and similar agentic coding environments
- Autonomous agent frameworks integrating with YoloScribe
- Custom orchestration systems requiring programmatic wiki access

---

## 2. Transport Layer

### HTTP-Based Communication
- **Protocol:** HTTP/HTTPS (mandatory HTTPS in production)
- **Base URL:** `https://<your-domain>/mcp/v1`
- **Content-Type:** `application/json` for request/response bodies

### Streaming
FastMCP's streamable HTTP transport is used (chunked transfer encoding, NDJSON).
SSE is available via FastMCP's built-in SSE support if clients request it.

### Timeout
Response timeout: 30 seconds per tool call.

---

## 3. Authentication & Authorization

### Mechanisms

**Supabase JWT (supported — recommended)**
- Header-based: `Authorization: Bearer <supabase-jwt>`
- JWT validated against Supabase JWKS endpoint
- User's site resolved from `user_site` table (5-minute in-memory cache)
- All OAuth token management (issuing, refreshing, revoking) is handled externally by Supabase or another identity provider — the MCP server only validates tokens

### How Clients Connect

**Supabase JWT:**
```bash
claude mcp add --transport http yoloscribe https://<your-domain>/mcp/v1/ \
  --header "Authorization: Bearer <supabase-jwt>"
```

### Permission Model
- Each authenticated user has a single site; all MCP operations are scoped to that site
- Cross-site operations are not supported

---

## 4. Core Tools & Capabilities

### 4.1 Wiki CRUD Operations

**`wiki_create`**
- **Parameters:** `page_path` (string), `content` (string)
- **Returns:** `{ page_path, url, created_at }`
- Creates content.md; writes default private settings.json; enqueues indexing

**`wiki_read`**
- **Parameters:** `page_path` (string), `include_metadata` (optional bool)
- **Returns:** `{ page_path, content, last_updated, [size_bytes, children] }`

**`wiki_update`**
- **Parameters:** `page_path` (string), `content` (string), `message` (optional string)
- **Returns:** `{ page_path, updated_at, message }`
- Enqueues indexing after write

**`wiki_delete`**
- **Parameters:** `page_path` (string), `reason` (optional string)
- **Returns:** `{ page_path, deleted_at, archived: true, reason }`
- Soft delete: copies to `{site}/.archive/{page_path}/content.md`, then deletes original

**`wiki_list`**
- **Parameters:** `page_path` (optional string), `recursive` (optional bool, default true), `limit` (optional int, default 100, max 500)
- **Returns:** `{ pages: [{path, title, updated_at, size_bytes}] }`
- Skips internal prefixes (`.agents`, `.skills`, `.mcp`, `.archive`, etc.)

### 4.2 Search & Retrieval

**`search_wiki`**
- **Parameters:** `query` (string), `page_path_prefix` (optional string), `limit` (optional int, default 20)
- **Returns:** `{ results: [{page_path, score, excerpt, updated_at}], total_hits }`
- Case-insensitive substring search across all content.md files in the user's site

**`search_semantic`**
- **Parameters:** `query` (string), `limit` (optional int, default 20), `min_score` (optional float, default 0.0)
- **Returns:** `{ results: [{page_path, similarity_score, content_preview}] }`
- Uses Bedrock embedding + S3 Vectors; filters results to user's site post-query
- Requires `S3_VECTORS_BUCKET` to be configured

### 4.3 Agent Session Management

These tools manage ephemeral agent sessions (distinct from agent.md runner definitions).
Session data is stored at `{site}/.mcp/agents/{agent_id}/`.

**`agent_create`**
- **Parameters:** `agent_name` (string), `description` (optional string), `config` (optional object)
- **Returns:** `{ agent_id, created_at, status }`

**`agent_get_status`**
- **Parameters:** `agent_id` (string)
- **Returns:** full metadata object from meta.json

**`agent_update_context`**
- **Parameters:** `agent_id` (string), `context` (object), `ttl` (optional int seconds)
- **Returns:** `{ agent_id, context_id, updated_at }`
- Overwrites previous context; also updates `last_activity` in meta.json

**`agent_get_context`**
- **Parameters:** `agent_id` (string), `context_id` (optional string)
- **Returns:** `{ context, context_id, retrieved_at }`
- Returns latest context (context_id is informational; only latest is stored)
- Raises error if TTL has expired

**`agent_list`**
- **Parameters:** `limit` (optional int, default 50, max 200)
- **Returns:** `{ agents: [{agent_id, name, status, last_activity}] }`

---

## 5. Implementation Notes

### Files
- `backend/mcp_server.py` — FastMCP app factory (`create_mcp_app`)
- `backend/main.py` — mounts the ASGI app at `/mcp/v1`
- `backend/pyproject.toml` — adds `fastmcp>=2.14.0` dependency

### S3 Layout (MCP-specific additions)
```
{site}/
  .mcp/agents/{uuid}/meta.json        # agent session metadata
  .mcp/agents/{uuid}/context.json     # agent session context (latest)
  .archive/{page}/content.md          # soft-deleted page content
```

### Limitations & Future Work
- `search_wiki` is a full S3 object scan — suitable for small sites; replace with OpenSearch for large deployments
- `search_semantic` over-fetches vectors (5× limit) and filters client-side for site scoping; S3 Vectors server-side metadata filtering would be more efficient
- Agent context stores only the latest version; version history is deferred
- Rate limiting is deferred (handle at ALB/API Gateway layer)
- OAuth Dynamic Client Registration (`/.well-known/oauth-authorization-server`) is deferred
