# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Local dev (Docker Compose — no AWS required)
```bash
cp .env.local .env        # fill in ANTHROPIC_API_KEY
docker compose up -d      # MinIO + ElasticMQ + backend + agent-runner + frontend
# Open http://localhost:5173 → redirects to /local/
```
See `INSTALL.md` for full details.

### Frontend (`frontend/`)
```bash
npm install          # install deps
npm run dev          # Vite dev server (http://localhost:5173), proxies /api → localhost:8000
npm run build        # tsc + vite build → dist/
npm run preview      # serve the built dist/
```

### Backend (`backend/`)
```bash
uv sync                                                         # install deps (pyproject.toml; no requirements.txt)
uv run --env-file ../.env uvicorn main:app --reload             # dev server on :8000
AWS_PROFILE=myprofile uv run --env-file ../.env uvicorn main:app --reload  # with a named AWS profile
```

Backend has no test suite yet. Lint/type-check: `uv run mypy main.py agents/`.

## Architecture

### Data model (S3)
Every "site" is an S3 prefix. The bucket layout is:

```
{site}/
  content.md                          # root page content
  settings.json                       # root page access-control settings
  {page}/content.md                   # child page content
  {page}/settings.json                # child page access-control settings
  .agents/{name}/agent.md             # root-page agent definition
  {page}/.agents/{name}/agent.md      # child-page agent definition
  .skills/{name}/skill.md             # skill instructions (site-scoped)
  .skills/{name}/mcp.json             # MCP server config for that skill
  .user/notifications.md              # owner notification inbox (access requests)
  .mcp/agents/{uuid}/meta.json        # remote MCP agent session metadata
  .mcp/agents/{uuid}/context.json     # remote MCP agent session context
  .archive/{page}/content.md          # soft-deleted page archive
```

`settings.json` schema: `{"visibility": "public"|"private"|"shared", "shared_with": [{"email": "...", "access": "view"|"write"}]}`
Default when absent: `{"visibility": "private", "shared_with": []}`

Skills are site-scoped (under `{site}/.skills/`). Agents are page-scoped (under `{page}/.agents/`).

### Frontend routing
- `SITE` is derived from the **first URL path segment** (e.g. `/knuth-home/` → `"knuth-home"`). Falls back to the `VITE_SITE` env var or `"default"` in dev.
- The **URL hash** controls which file is loaded:
  - `#/.agents/{name}` → `.agents/{name}/agent.md`
  - anything else → `content.md`
- `API_BASE` is always `/api` in dev (Vite proxy); `VITE_API_BASE` (set at build time) in production.

### Backend path safety
All writable paths must pass `SAFE_PATH` in `main.py`. Allowed patterns:
- `content.md`
- `{page}/content.md`
- `settings.json`
- `{page}/settings.json`
- `.agents/{name}/agent.md`
- `{page}/.agents/{name}/agent.md`
- `.user/search.md`
- `.user/notifications.md`

This is validated on both `PUT /content` and `POST /chat`.

### Agent framework (Strands Agents)
All agents inherit from `BaseAgent` (`backend/agents/base.py`), which itself inherits from `strands.Agent`. Each class has a `SYSTEM_PROMPT` class variable (a Python format-string; placeholders are filled via `**prompt_vars` in the constructor).

The model is `AnthropicModel` from `strands.models.anthropic`, defaulting to `DEFAULT_MODEL = "claude-opus-4-6"` (see `base.py`). The model can be overridden at runtime via the `YOLOSCRIBE_MODEL` env var.

**Agent hierarchy:**

```
ChatAgent (orchestrator)
├── content_writer @tool → ContentWriterAgent
├── creator        @tool → CreatorAgent
├── page_creator   @tool → PageCreatorAgent
└── runner         @tool → RunnerAgent
```

`ChatAgent.run()` is the entry point called by the FastAPI `/chat` route. It builds fresh sub-agent `@tool` functions per request (so each gets the right `site`/`page_path` context), creates an inner `strands.Agent` with those tools, and calls it. Updated content is passed back through a shared mutable dict (`shared["updated_content"]`).

**Sub-agents:**
- `ContentWriterAgent` — uses `S3Tools.get_content` / `put_content` to read and write `content.md`.
- `CreatorAgent` — uses `S3Tools.list_skills`, `get_skill`, `put_agent` to create `agent.md` files.
- `PageCreatorAgent` — uses `S3Tools.create_page` to initialise new page prefixes in S3.
- `RunnerAgent` — sends an SQS message (bucket + keys + prompt) to queue async agent execution.

**`S3Tools`** (`backend/agents/base.py`) is a class-based strands tool container. Its methods are passed directly as tools to agent constructors.

### `agent.md` format
```markdown
# Agent: {name}

## Description

{description}

## Skills

- {skill-name}
```

Parsed at runtime by the async worker (not the web process).

### `mcp.json` format
Standard MCP config with `mcpServers` map. Used by the async SQS worker to spawn MCP subprocesses. Environment variable placeholders `${VAR}` are substituted at load time.

### Remote MCP Server (`backend/mcp_server.py`)
Mounted at `/mcp/v1` in the FastAPI app. Provides wiki CRUD, semantic search, and agent session management for Claude Code and other MCP-compatible AI agents.

**Auth:** Every request must carry a Supabase JWT as `Authorization: Bearer <token>`. The JWT is validated against the Supabase JWKS endpoint; the user's site is resolved from the `user_site` table (5-minute in-memory cache).

**Tools:** `wiki_create`, `wiki_read`, `wiki_update`, `wiki_delete`, `wiki_list`, `search_wiki`, `search_semantic`, `agent_create`, `agent_get_status`, `agent_update_context`, `agent_get_context`, `agent_list`.

All operations are scoped to the authenticated user's site. Agent sessions (`.mcp/agents/{uuid}/`) are distinct from agent.md runner definitions.

**Connect with Claude Code:**
```bash
claude mcp add --transport http yoloscribe https://<your-domain>/mcp/v1 \
  --header "Authorization: Bearer <supabase-jwt>"
```

## Environment Variables

| Variable | Where | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | backend | Required for Claude API (used by strands AnthropicModel) |
| `S3_BUCKET` | backend | S3 bucket name |
| `ALLOWED_ORIGINS` | backend | Comma-separated CORS origins |
| `LOG_LEVEL` | backend + agent-runner | Root log level (`DEBUG`\|`INFO`\|`WARNING`\|`ERROR`); default: `INFO` |
| `AWS_PROFILE` | backend | Optional named AWS profile |
| `YOLOSCRIBE_MODEL` | backend + agent-runner | Global model key fallback (see model registry below) |
| `YOLOSCRIBE_CHAT_MODEL` | backend | ChatAgent (orchestrator) model key |
| `YOLOSCRIBE_WRITER_MODEL` | backend | ContentWriterAgent model key (default: `haiku`) |
| `YOLOSCRIBE_CREATOR_MODEL` | backend | CreatorAgent / PageCreatorAgent model key (default: `sonnet`) |
| `YOLOSCRIBE_RUNNER_MODEL` | agent-runner | agent-runner default when `agent.md` has no `## Model` section |
| `SQS_QUEUE_URL` | backend | SQS queue URL for async agent execution (RunnerAgent) |
| `S3_VECTORS_BUCKET` | backend | S3 Vectors bucket name (for search index and deletion on account delete) |
| `S3_VECTORS_INDEX_NAME` | backend | S3 Vectors index name (default: `yoloscribe`) |
| `S3_ENDPOINT_URL` | backend + agent-runner | Custom S3 endpoint (MinIO for local dev) |
| `SQS_ENDPOINT_URL` | backend + agent-runner | Custom SQS endpoint (ElasticMQ for local dev) |
| `LOCAL_MODE` | backend | Set `true` to bypass Supabase auth, IAM/K8s, and Secrets Manager |
| `LOCAL_SITE_NAME` | backend | Site name used when `LOCAL_MODE=true` (default: `local`) |
| `LOCAL_USER_ID` | backend | User ID used when `LOCAL_MODE=true` |
| `LOCAL_RUNNER` | agent-runner + indexer | Set `true` to run agent/index jobs inline (no K8s) |
| `VITE_API_BASE` | frontend build | ALB URL for production |
| `VITE_SITE` | frontend dev | Override site name in dev |

### Model registry keys

Valid model keys for all `YOLOSCRIBE_*_MODEL` env vars and the `## Model` section in `agent.md`:

| Key | Provider | Model |
|---|---|---|
| `haiku` | Anthropic | claude-haiku-4-5-20251001 |
| `sonnet` | Anthropic | claude-sonnet-4-6 |
| `opus` | Anthropic | claude-opus-4-6 |
| `bedrock-haiku` | Bedrock | claude-haiku-4-5 (cross-region) |
| `bedrock-sonnet` | Bedrock | claude-sonnet-4-6 (cross-region) |
| `bedrock-opus` | Bedrock | claude-opus-4-6 (cross-region) |

Unrecognised keys fall back to `sonnet`.

Copy `env.example` to `.env` at the project root for local development. All scripts and the backend dev server load from there.
