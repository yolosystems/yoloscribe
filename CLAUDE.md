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
  .user/ingest/{filename}             # queued document awaiting routing
  .user/ingest/.provenance/{f}.json   # staged provenance for a queued document (YOL-552)
  .user/ingest/processed/{filename}   # routed documents
  {page}/.media/{filename}.json       # page asset metadata; carries landed provenance
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

**This flow is specific to `VITE_AUTH_PROVIDER=supabase`.** Under `oidc` the identity provider owns sign-up entirely; there is no invite link and no expired-invite page (`getInviteLinkError()` in `App.tsx` returns null for non-Supabase providers). Provisioning still happens on first successful sign-in through the same `GET /my-site` → `OnboardingView` → `POST /provision` path.

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

### Internal endpoints (`/internal/*`)

Backend-to-backend routes in `backend/routers/internal.py`, authenticated by a shared secret in the `X-Internal-Auth` header (`backend/internal_auth.py`) rather than a user JWT or API token:

| Route | Caller | Secret |
|---|---|---|
| `POST /internal/runs/mint` | agent-runner `polling_worker` | `INTERNAL_MINT_SECRET` |
| `POST /internal/messaging/link` | messaging-bot (`/setup`) | `MESSAGING_BOT_SECRET` |
| `GET /internal/messaging/binding` | messaging-bot | `MESSAGING_BOT_SECRET` |
| `POST /internal/messaging/message` | messaging-bot | `MESSAGING_BOT_SECRET` |
| `POST /internal/messaging/ingest/{upload,trigger}` | messaging-bot | `MESSAGING_BOT_SECRET` |

**Two separate secrets, deliberately.** `/internal/runs/mint` takes an arbitrary `site` + `user_id`, so anything holding that secret can act as any user. The messaging bot processes untrusted input from chat platforms and must never reach it. `install_messaging_bot.sh` refuses to deploy if the two values match.

**These are blocked at the ALB.** The `block-internal-paths` WAF rule denies `^/internal(/|$)` on every host, and sits at priority 0 because `allow-api-dev-host` is a *terminating* Allow that would otherwise shadow it (see `infra/waf/README.md`). Callers reach these routes over cluster DNS — pod → ClusterIP → pod never traverses the load balancer — so in-cluster clients must be configured with the **internal service address**, not the public hostname.

The WAF is not the auth boundary: any pod in the cluster can reach a ClusterIP, which is why these routes still check a secret. The pairing is what matters — a leaked secret isn't remotely exploitable, and in-cluster reachability alone doesn't authorize anything.

**Messaging resolution.** The bot holds no user API token: it names a channel, and the backend resolves channel → `api_token_id` → `(user_id, site)` through `MessagingConfigRepository.get_by_channel` + `ApiTokenRepository.get_by_id`. Bindings store no credential, so there is nothing to encrypt at rest, and revoking or expiring a token disconnects its channels automatically (`get_by_id` returns `None`). New sub-handlers should reuse `routers.message.handle_message` / `routers.ingest.*` rather than duplicating logic — note that `handle_message` is split out of the `/message` route specifically so the internal path does **not** inherit the IP-keyed rate limit, since all bot traffic shares one pod IP.

### Agent framework (Strands Agents)
All agents inherit from `BaseAgent` (`backend/agents/base.py`), which itself inherits from `strands.Agent`. Each class has a `SYSTEM_PROMPT` class variable (a Python format-string; placeholders are filled via `**prompt_vars` in the constructor).

The model is built by `yoloscribe_io.models.build_strands_model(key)` — an OpenAI-compatible Strands model pointed at the LiteLLM proxy (`LITELLM_BASE_URL`); see [Model routing (LiteLLM)](#model-routing-litellm) below. The key defaults to `DEFAULT_MODEL_KEY = "sonnet"` and is overridable per agent via `YOLOSCRIBE_*_MODEL` env vars or the `model:` frontmatter field.

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
type: page|ingest|notification|eval_annotator|consolidation  # optional; explicit agent class dispatch (omit to use heuristics)
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
- `consolidation` — platform-provisioned scheduled agent that runs the Librarian memory consolidation pass; derives inductive/abductive conclusions from accumulated signals, decays stale conclusions, and generates a population lint report. NOT user-creatable; automatically provisioned as `librarian-consolidation` when memory.md gets its first write.

The `type:` field drives explicit dispatch in the agent-runner. When absent, the runner falls back to heuristics: `trigger == on_notify` → `notification`; `page_path == .user/ingest` → `ingest`; otherwise → `page`.

**Eval annotation flow** (`eval_log: true` agents):
1. After a successful run (when `OTEL_EXPORTER_OTLP_ENDPOINT` is set), the trace fetcher polls Phoenix via `PHOENIX_API_ENDPOINT` for spans with the run's `session.id`, formats an annotation log (`runs/YYYY-MM-DD-{8hex}.md`), and writes it to S3.
2. The owner can view, edit, and fill in Rating/Notes/Correction in the annotation log via the "runs" button in `AgentsList`.
3. Saving the annotation log triggers the platform-provisioned `phoenix-annotator` (`type: eval_annotator`) agent via SQS.
4. `EvalAnnotatorAgent` reads the log, calls the `annotate_trace` MCP tool on the backend which validates site ownership and writes span labels to Phoenix `/v1/span_annotations`.

### Ingest provenance (YOL-552)

Where a document came from and what became of it, in two lifecycle stages (`libs/yoloscribe_io/provenance.py`):

- **Staged** — written by `POST /ingest/upload` (optional `intent` and `source_url` params) to `{site}/.user/ingest/.provenance/{filename}.json`, *before* the bytes land. This is the only moment the caller's purpose and the document's origin are knowable; neither can be recovered afterwards. `ingest_list_pending` filters the `.provenance/` prefix so records never look like documents awaiting routing.
- **Landed** — the IngestAgent calls `ingest_mark_processed(filename, page_path)`, which lands the record on the destination page as the media-asset sidecar at `{site}/{page}/.media/{filename}.json`, nested under a `provenance` key, adding the routing outcome, the extractor that produced the text, and the retention choice.

`Retention` is `delete` | `yoloscribe` | `external`; **default is `yoloscribe`** because deleting the original bytes forfeits re-extraction, which is the "appreciates rather than depreciates" property PageIndex is built on. `external` (copy to Drive/SharePoint) needs tool OAuth and is not implemented.

`SourceStatus` is `none` | `unverified` | `verified` | `mismatch`. **`source_url` is an assertion by whoever ingested the document** — nothing stops a caller naming a public URL while uploading a restricted file — so only `verified` may act as an access-control anchor (`Provenance.gates_access`). Verification means fetching the claimed source through the ingesting user's enrolled tool and fingerprinting it against the bytes; that is **not built yet**, so everything currently records as `unverified`. YOL-553 depends on it.

The agent reads staged intent via the `ingest_read_intent` tool and is instructed to reuse it as the `reason` on the resulting `wiki_write`, tying provenance to YOL-527's write-reason discipline.

### KM signal delivery to YoloBrain (YOL-558)

Knowledge-management signals fan out through `signal_sinks.dispatch(site, signal_type, params, user_id)` to a process-wide `CompositeSignalSink`. `WebhookSignalSink` (site-keyed, no-op without configured targets) is always present; `YoloBrainSignalSink` is added only when **both** `YOLOBRAIN_API_URL` and `YOLOBRAIN_INTERNAL_SECRET` are set — half-configured logs a warning and delivers nothing, rather than POSTing unauthenticated or authenticating against a default URL.

**Routing is by user, not by site.** YoloBrain's workspace is a user (`engine.submit_signal(user_sub, ...)`), but `SignalSink.emit` is site-keyed. The subject is the **actor** taken from the mutation event payload — `WikiPageMarkdownFile._payload()` already carries `user_id` and `KMSignalHandler` forwards it. Deliberately *not* a site→owner lookup: `UserSiteRepository` has no reverse index (the DynamoDB table is keyed on `user_id`, so it would need a new GSI), and it would attribute a shared-write user's edit to the site owner instead of the person who made it.

A signal with no actor is **skipped** by this sink rather than sent with an empty subject, which would file it under a workspace keyed on `""`. Some write paths genuinely have no actor (e.g. `MessagingAgent`'s unattributed `wiki.write`); site-keyed sinks still receive those.

Delivery is best-effort and off the write path — `dispatch` offloads to a background thread and never raises, so a YoloBrain outage costs signals, never wiki writes.

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

**Caller tiers (YOL-525/526).** The registry is split by caller, and every `@mcp.tool()` declares its tier via `tags`:

- `TIER_INTERNAL` — run-token callers (the first-party agent-runner)
- `TIER_EXTERNAL` — user JWT or `as_` static key (the SPA, Claude Code, 3P clients)

A tool may carry both. **A tool with no tier tag is treated as internal — fail closed**; `backend/tests/test_mcp_tool_tiers.py` fails if any registered tool is untagged. `_ToolTierMiddleware` both hides out-of-tier tools from `list_tools` and refuses them at call time, raising the same `NotFoundError` FastMCP produces for an unregistered tool (a distinct "forbidden" would confirm the tool exists).

**Write reason (YOL-527).** `wiki_create`, `wiki_update`, and `wiki_archive` take a **required** `reason` — one line on why the write is happening. YoloScribe only ever sees a committed tool call, never the conversation that produced it, so rather than trying to recover the transcript the tool signature demands the distillate. This is the commit-message pattern, and it is what lets a later corrective edit be read as a labeled example (feeds YOL-518). `_require_reason` rejects empty and placeholder values; the real pressure comes from the parameter descriptions. The reason rides `WikiPageMarkdownFile`'s mutation event → `KMSignalHandler` → the `content_routed` / `page_structured` KM signal params, and also surfaces in the owner's notification entry via `NotificationBusHandler`. **Caveat:** YoloBrain's catalog does not declare `reason` on those two params models and pydantic defaults to `extra="ignore"`, so it is dropped on arrival until that repo is updated (YOL-550).

**External surface** (what a 3P assistant sees):
- Wiki: `wiki_create`, `wiki_read`, `wiki_update`, `wiki_archive`, `wiki_list`, `wiki_versions`, `wiki_diff`, `empty_archive`
- Search: `search`
- Read-only introspection: `agent_read`, `agent_list`, `skill_list`, `skill_read`, `list_skill_tools` — the last returns all tools available to agents on this site, derived from each skill's `tools:` frontmatter declaration, as a flat list of `{name, skill}` pairs; useful for discovering whether a particular tool (e.g. a Linear or GitHub tool) is already installed before asking the user to create a new skill

**Internal-only surface:** `ingest_*` (including `ingest_read_provenance` / `ingest_record_provenance`), `run_log_append`, `propose_page_change`, `notify`, `annotate_trace`, `emit_signal`, `read_memory` / `write_memory`, `read_archetypes` / `write_archetypes`, `read_signal_log`, plus **all agent and skill authoring** — `agent_create`, `agent_create_page`, `agent_create_ingest`, `agent_create_notification`, `agent_update`, `agent_delete`, `skill_create`, `skill_update`, `skill_delete`.

Authoring went internal-only under YOL-526: those tools existed so a 3P assistant could push definitions into YoloScribe, and that use case ends when YoloScribe authors `agent.md` itself. They stay registered rather than deleted because the platform's own learned-agent path (YOL-518) writes through them — the shrink is to the external surface, not to the capability. `agent.md` remains the portable IR either way. Note the tiers are not nested: `empty_archive` is external-only precisely because no agent should hold it.

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
| `LITELLM_BASE_URL` | backend + agent-runner | **Required.** LiteLLM proxy OpenAI endpoint (e.g. `http://litellm:4000/v1`) — the single model path (YOL-512) |
| `LITELLM_API_KEY` | backend + agent-runner | Key presented to the LiteLLM proxy (matches its master key, or a per-user virtual key under YOL-513) |
| `YOLOSCRIBE_MODEL` | backend + agent-runner | Global model-key fallback (a LiteLLM `model_name`; see Model routing below) |
| `YOLOSCRIBE_CHAT_MODEL` | backend | ChatAgent (orchestrator) model key |
| `YOLOSCRIBE_WRITER_MODEL` | backend | ContentWriterAgent model key (default: `haiku`) |
| `YOLOSCRIBE_CREATOR_MODEL` | backend | CreatorAgent / PageCreatorAgent model key (default: `sonnet`) |
| `YOLOSCRIBE_RUNNER_MODEL` | agent-runner | agent-runner default when `agent.md` has no `## Model` section |
| `YOLOSCRIBE_MEMORY_REASONER_MODEL` | agent-runner | Per-signal `HaikuMemoryReasoner` model key (default: `haiku`); Anthropic-provider keys only |
| `YOLOSCRIBE_CONSOLIDATION_REASONER_MODEL` | agent-runner | Nightly `ConsolidationMemoryReasoner` model key (default: `sonnet`); Anthropic-provider keys only |
| `YOLOSCRIBE_MODEL_BASE_URL` | agent-runner | Optional Anthropic API base URL for the Librarian memory reasoners only (main model path uses `LITELLM_BASE_URL`); slated for removal with the Librarian (YOL-509) |
| `LITELLM_MCP_URL` | backend | Public base URL of the LiteLLM MCP gateway; tool OAuth enrollment runs against `{LITELLM_MCP_URL}/mcp/{tool}` (delegated PKCE). Must be publicly reachable for the browser authorize redirect (YOL-505) |
| `SQS_QUEUE_URL` | backend | SQS queue URL for async agent execution (RunnerAgent) |
| `PHOENIX_API_ENDPOINT` | backend + agent-runner | Base URL for the Arize Phoenix REST API (e.g. `http://phoenix:6006`); enables `annotate_trace` MCP tool and eval annotation log post-processing |
| `YOLOBRAIN_API_URL` | backend | In-cluster base URL of YoloBrain's API (e.g. `http://yolobrain-api.yolo.svc.cluster.local:8080`); enables `YoloBrainSignalSink`. **Never the public hostname** — `/internal/*` is blocked at YoloBrain's edge (YOL-558) |
| `YOLOBRAIN_INTERNAL_SECRET` | backend | Shared secret for YoloBrain's `POST /internal/signals`; must match its `INTERNAL_SIGNAL_SECRET`. **Can act as any user** — never share with anything processing untrusted input |
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
| `INTERNAL_MINT_SECRET` | backend + agent-runner | Shared secret for `POST /internal/runs/mint` (`X-Internal-Auth`) |
| `MESSAGING_BOT_SECRET` | backend + messaging-bot | Shared secret for `/internal/messaging/*`. **Must differ from `INTERNAL_MINT_SECRET`** — see below |
| `VITE_API_BASE` | frontend build | ALB URL for production |
| `VITE_SITE` | frontend dev | Override site name in dev |
| `VITE_AUTH_PROVIDER` | frontend build | `supabase` (default) \| `oidc` — selects the browser auth client |
| `VITE_OIDC_CONFIG_URL` | frontend build | OIDC discovery URL (`oidc` only); endpoints are read from it |
| `VITE_OIDC_CLIENT_ID` | frontend build | Public/SPA client ID (`oidc` only); never a confidential client |
| `VITE_OIDC_SCOPE` | frontend build | Optional; default `openid email profile offline_access` |
| `VITE_OIDC_TOKEN` | frontend build | Optional; `id` (default) \| `access` — which token is sent as the bearer |

### Model routing (LiteLLM)

As of YOL-512, **all** model calls route through a **LiteLLM proxy** — there is no native per-provider model building in YoloScribe. `yoloscribe_io.models.build_strands_model(key)` returns an OpenAI-compatible Strands model pointed at `LITELLM_BASE_URL`, passing the model key straight through as the OpenAI `model`. Both backend and agent-runner use this single shared builder (`libs/yoloscribe_io/yoloscribe_io/models.py`); there is no longer a `backend/agents/models.py` or an inline `_MODEL_REGISTRY` in the runner.

Provider / model / credential resolution lives in the **LiteLLM config**, not in YoloScribe code. The model keys used by `YOLOSCRIBE_*_MODEL` env vars and the `model:` frontmatter field (`haiku`, `sonnet`, `opus`, `glm`, `bedrock-*`) are the `model_name` entries in `infra/litellm/config.example.yaml` — edit that file (or your deployed LiteLLM config) to add/change models, credentials, or providers. `LITELLM_BASE_URL` must be set; there is no per-provider fallback. Empty keys resolve to `DEFAULT_MODEL_KEY` (`sonnet`).

**Policy that stays in YoloScribe:** the per-agent-type defaults (`YOLOSCRIBE_CHAT_MODEL`, `YOLOSCRIBE_WRITER_MODEL`, `YOLOSCRIBE_CREATOR_MODEL`, `YOLOSCRIBE_RUNNER_MODEL` → `YOLOSCRIBE_MODEL` fallback) — which agent prefers which tier — via `resolve_model_key(...)`.

**Embeddings deliberately bypass LiteLLM.** The indexer and the search backend call **Bedrock directly** for embeddings (`amazon.titan-embed-*` → S3 Vectors) — this is intentional, not an oversight. The embedding model is effectively fixed (switching it forces a full re-index), so the provider-flexibility LiteLLM buys has no value here; the tradeoff is that embedding spend isn't metered against a user's virtual key (fine — indexing is background platform work, not user chat). Route it through LiteLLM only if you later want unified spend/observability.

Deployment: LiteLLM is a separate service (its official `berriai/litellm-helm` chart, or the `litellm` service in `docker-compose.yml` for local dev). See YOL-505 for the full deployment approach.

Copy `env.example` to `.env` at the project root for local development. All scripts and the backend dev server load from there.
