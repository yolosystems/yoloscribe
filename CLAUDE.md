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
  .skills/{name}/SKILL.md             # skill instructions (site-scoped)
  .skills/{name}/mcp.json             # MCP server config for that skill
  .user/notifications.md              # owner notification inbox (platform-controlled; read-only in UI)
  .archive/{page}/content.md          # soft-deleted page archive

.tools/{name}/mcp.json                # tool MCP server config (bucket root, shared across all sites)
.tools/{name}/oauth_client.json       # OAuth client credentials for the tool (optional)
.tools/{name}/tool.md                 # human/agent-readable description of the tool (optional)
```

`settings.json` schema: `{"visibility": "public"|"private"|"shared", "shared_with": [{"email": "...", "access": "view"|"write"}]}`
Default when absent: `{"visibility": "private", "shared_with": []}`

Skills are site-scoped (under `{site}/.skills/`). Agents are page-scoped (under `{page}/.agents/`). Tools are bucket-scoped (under `.tools/` at the bucket root, shared across all sites).

### Frontend routing
- `SITE` is derived from the **first URL path segment** (e.g. `/knuth-home/` → `"knuth-home"`). Falls back to the `VITE_SITE` env var or `"default"` in dev.
- The **URL hash** controls which file is loaded:
  - `#/.agents/{name}` → `.agents/{name}/agent.md`
  - anything else → `content.md`
- `API_BASE` is always `/api` in dev (Vite proxy); `VITE_API_BASE` (set at build time) in production.

### Invite / magic link flow (waitlist sign-up)
New users arrive at `app.yoloscribe.com` (or `app-dev.yoloscribe.com`) via a Supabase invite magic link sent by the waitlist Edge Function. Supabase's implicit flow (`flowType: 'implicit'` in `auth.ts`) automatically processes the `#access_token=...&type=invite` hash on page load, fires `onAuthStateChange`, and establishes the session. The existing routing then calls `GET /my-site` → null → `OnboardingView` → `POST /provision`.

**Required Supabase project configuration (Auth → URL Configuration):**
- Add `https://app.yoloscribe.com` and `https://app-dev.yoloscribe.com` to the **Redirect URLs** allowlist. Without this, `inviteUserByEmail` calls from the Edge Function will be rejected.
- The invite TTL is configurable under Auth → Email → OTP Expiry (default 24 h; consider raising to 72 h or more).

If the invite link is expired or already used, Supabase redirects back with `#error=access_denied&error_description=...` in the URL hash. `App.tsx` detects this on initial load (before Supabase clears the hash) and renders a friendly error page linking back to the marketing site.

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
type: page|ingest|notification|eval_annotator  # optional; explicit agent class dispatch (omit to use heuristics)
schedule: 0 9 * * *   # required when trigger: schedule
timezone: America/New_York  # optional; defaults to UTC
skills:
  - {skill-name}
model: sonnet          # optional; overrides server default
confirm_before_write: true  # optional; when true, writes go to .proposed.content.md
eval_log: true         # optional; default false; enables eval annotation log post-processing after each run
---

{description}
```

**Trigger types:**
- `manual` — run on demand only
- `schedule` — K8s CronJob (requires `schedule:` cron expression); `concurrencyPolicy: Forbid` prevents overlapping runs
- `on_write` — fires when the agent's page `content.md` is updated; agents are looked up under the page's `.agents/` directory
- `on_notify` — fires when a new entry is appended to the site's `notifications.md`; agents are looked up under the site root's `.agents/` directory; `agent_success` and `agent_failure` events never trigger `on_notify` to prevent feedback loops

**Agent types (`type:` field):**
- `page` — wiki page agent; reads and writes `content.md` on a wiki page (default when `type:` is absent and trigger is not `on_notify` and page is not `.user/ingest`)
- `ingest` — ingest agent; processes content staged in `.user/ingest/` and routes it to wiki pages using semantic search against the wiki's own structure; must be placed on `.user/ingest`. The page `.user/ingest/content.md` serves as an owner-editable routing instructions file — plain-text hints (e.g. "meeting notes go under meetings/") that are injected into the agent's system prompt on every run and take priority over the agent's own judgement.
- `notification` — notification agent; handles entries in `.user/notifications.md`; must use `trigger: on_notify` and be placed at the site root
- `eval_annotator` — platform-provisioned annotation agent; reads a run log at `{page}/.agents/{name}/runs/{YYYY-MM-DD}-{8hex}.md`, extracts Rating/Notes/Correction fields, and calls the `annotate_trace` MCP tool to write span labels to Phoenix. NOT user-creatable; automatically provisioned as `phoenix-annotator` when any agent with `eval_log: true` is saved.

The `type:` field drives explicit dispatch in the agent-runner. When absent, the runner falls back to heuristics: `trigger == on_notify` → `notification`; `page_path == .user/ingest` → `ingest`; otherwise → `page`.

**Eval annotation flow** (`eval_log: true` agents):
1. After a successful run (when `OTEL_EXPORTER_OTLP_ENDPOINT` is set), the trace fetcher polls Phoenix via `PHOENIX_API_ENDPOINT` for spans with the run's `session.id`, formats an annotation log (`runs/YYYY-MM-DD-{8hex}.md`), and writes it to S3.
2. The owner can view, edit, and fill in Rating/Notes/Correction in the annotation log via the "runs" button in `AgentsList`.
3. Saving the annotation log triggers the platform-provisioned `phoenix-annotator` (`type: eval_annotator`) agent via SQS.
4. `EvalAnnotatorAgent` reads the log, calls the `annotate_trace` MCP tool on the backend which validates site ownership and writes span labels to Phoenix `/v1/span_annotations`.

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
| `confirm_page_change` | agent-runner (propose mode; DOES trigger `on_notify`) |
| `ingest_unrouted` | IngestAgent (`notify_owner` tool; DOES trigger `on_notify`) |
| `ingest_start` | IngestAgent lifecycle — fired at the start of every run (DOES trigger `on_notify`) |
| `ingest_end` | IngestAgent (`ingest_complete` tool) — fired when all files are processed; payload includes `summary` of what was routed (DOES trigger `on_notify`) |

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

### Tool config format (`.tools/{name}/`)
Each tool in the bucket-root `.tools/` directory has up to three files:

**`mcp.json`** (required) — MCP server config. Two transport patterns:
```json
// Remote HTTP tool (OAuth-based, e.g. Linear, GitHub, Slack)
{ "mcpServers": { "linear": { "url": "https://mcp.linear.app/mcp", "transport": "streamable-http", "auth": "oauth" } } }

// Stdio subprocess tool (e.g. notification-mcp)
{ "mcpServers": { "notifications": { "command": "notification-mcp" } } }
```

**`oauth_client.json`** (optional) — OAuth app credentials for tools with `"auth": "oauth"`. Schema varies by provider but typically `{"client_id": "...", "client_secret_name": "..."}` where `client_secret_name` is a Secrets Manager key.

**`tool.md`** (optional) — human/agent-readable description: use cases, available capabilities, auth requirements. Follows the pattern in `mcp-tools/` in the repo. Not machine-parsed — serves as documentation for agents browsing available tools and for sysadmins configuring the deployment.

The `mcp-tools/` directory in the repo is the source of truth for tool configs. Sysadmins deploy these files to `.tools/{name}/` in S3 to enable a tool across all sites.

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
| `YOLOSCRIBE_MODEL_BASE_URL` | backend + agent-runner | Optional base URL for the Anthropic API client (e.g. a Bedrock Mantle endpoint); when set, all Anthropic model calls are directed to this URL instead of the default |
| `SQS_QUEUE_URL` | backend | SQS queue URL for async agent execution (RunnerAgent) |
| `PHOENIX_API_ENDPOINT` | backend + agent-runner | Base URL for the Arize Phoenix REST API (e.g. `http://phoenix:6006`); enables `annotate_trace` MCP tool and eval annotation log post-processing |
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
| `glm` | OpenAI (Bedrock Mantle) | zai.glm-5 |
| `bedrock-haiku` | Bedrock | claude-haiku-4-5 (cross-region) |
| `bedrock-sonnet` | Bedrock | claude-sonnet-4-6 (cross-region) |
| `bedrock-opus` | Bedrock | claude-opus-4-6 (cross-region) |

Unrecognised keys fall back to `sonnet`.

Copy `env.example` to `.env` at the project root for local development. All scripts and the backend dev server load from there.
