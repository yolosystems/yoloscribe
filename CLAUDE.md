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
  .user/notifications.md              # owner notification inbox (platform-controlled; read-only in UI)
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

`.user/notifications.md` is **not** in SAFE_PATH — it is platform-controlled and may only be written by `notifications.write_notification()`. This is validated on both `PUT /content` and `POST /chat`.

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

Full format (definition agents) — all metadata in YAML frontmatter, free-form description in the body:
```markdown
---
trigger: manual|schedule|on_write|on_notify
name: {name}
schedule: 0 9 * * *   # required when trigger: schedule
timezone: America/New_York  # optional; defaults to UTC
scope:                 # optional; glob patterns for cross-page agents
  - "**"
skills:
  - {skill-name}
model: sonnet          # optional; overrides server default
---

{description}
```

Pointer agents (on_write subscriptions) use frontmatter only — no body:
```markdown
---
trigger: on_write
ref: {page_path}/.agents/{agent_name}/agent.md
---
```

**Trigger types:**
- `manual` — run on demand only
- `schedule` — K8s CronJob (requires `schedule:` cron expression); `concurrencyPolicy: Forbid` prevents overlapping runs
- `on_write` — fires when the agent's page `content.md` is updated; agents are looked up under the page's `.agents/` directory
- `on_notify` — fires when a new entry is appended to the site's `notifications.md`; agents are looked up under the site root's `.agents/` directory; `agent_success` and `agent_failure` events never trigger `on_notify` to prevent feedback loops

**Notification system** (`backend/notifications.py`):

`write_notification(site, event_type, payload, *, user_id="")` is the sole entry point for writing to `.user/notifications.md`. Entry format:
```
## YYYY-MM-DD HH:MM UTC — {event_type}

key: value
...
```

Event types and their sources:
| Event | Source |
|---|---|
| `access_requested` | `POST /request-access` |
| `page_shared` | `PUT /settings` |
| `page_unshared` | `PUT /settings` |
| `page_access_changed` | `PUT /settings` |
| `page_visibility_changed` | `PUT /settings` |
| `agent_success` | agent-runner (never triggers `on_notify`) |
| `agent_failure` | agent-runner + polling_worker (never triggers `on_notify`) |

**Backward-compatible old format** (still parseable but no longer generated):
```markdown
---
trigger: manual|schedule|on_write
---

# Agent: {name}

## Description

{description}

## Skills

- {skill-name}

## Model

sonnet
```

The schema is defined in `backend/agent_md.py` (MCP server) and `agent-runner/agent_runner/parse.py` (runner). Both must stay in sync — neither can import the other (separate packages). Parsed at runtime by both the MCP server and the async worker.

### `mcp.json` format
Standard MCP config with `mcpServers` map. Used by the async SQS worker to spawn MCP subprocesses. Environment variable placeholders `${VAR}` are substituted at load time.

### Remote MCP Server (`backend/mcp_server.py`)
Mounted at `/mcp/v1` in the FastAPI app. Provides wiki CRUD, semantic search, and agent definition management for Claude Code and other MCP-compatible AI agents.

**Auth:** Every request must carry a Supabase JWT as `Authorization: Bearer <token>`. The JWT is validated against the Supabase JWKS endpoint; the user's site is resolved from the `user_site` table (5-minute in-memory cache).

**Tools:**
- Wiki: `wiki_create`, `wiki_read`, `wiki_update`, `wiki_delete`, `wiki_list`
- Search: `search_wiki`, `search_semantic`
- Agents: `agent_create`, `agent_read`, `agent_update`, `agent_delete`, `agent_list`
- Skills: `skill_list`, `skill_create`, `skill_update`
- Introspection: `list_skill_tools` — returns all tools available to agents on this site, derived from each skill's `tools:` frontmatter declaration; result is a flat list of `{name, skill}` pairs, useful for discovering whether a particular tool (e.g. a Linear or GitHub tool) is already installed before asking the user to create a new skill

All operations are scoped to the authenticated user's site. Agent tools manage `agent.md` definition files on wiki pages (not session state). `agent_list` accepts a `page_path` for page-scoped listing, or `site_wide=True` to list all agents across the site.

**Connect with Claude Code:**
```bash
claude mcp add --transport http yoloscribe https://<your-domain>/mcp/v1/ \
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
| `CLOUDFRONT_SIGNING_KEY_ID` | backend | CloudFront key pair ID for signed-cookie media auth (e.g. `K2JCJMDEHXQW5F`) |
| `CLOUDFRONT_MEDIA_DOMAIN` | backend | CloudFront domain for video/audio assets; falls back to `CLOUDFRONT_DOMAIN` |
| `CLOUDFRONT_MEDIA_DISTRIBUTION_ID` | backend | CloudFront distribution ID for the media distribution; enables cache invalidation on asset delete |
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

Valid model keys for all `YOLOSCRIBE_*_MODEL` env vars and the `model:` frontmatter field in `agent.md`:

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
