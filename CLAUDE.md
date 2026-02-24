# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Frontend (`frontend/`)
```bash
npm install          # install deps
npm run dev          # Vite dev server (http://localhost:5173), proxies /api → localhost:8000
npm run build        # tsc + vite build → dist/
npm run preview      # serve the built dist/
```

### Backend (`backend/`)
```bash
uv sync                                              # install deps (pyproject.toml; no requirements.txt)
uv run uvicorn main:app --reload                     # dev server on :8000
AWS_PROFILE=myprofile uv run uvicorn main:app --reload  # with a named AWS profile
```

Backend has no test suite yet. Lint/type-check: `uv run mypy main.py agents/`.

## Architecture

### Data model (S3)
Every "site" is an S3 prefix. The bucket layout is:

```
{site}/
  content.md                          # root page content
  {page}/content.md                   # child page content
  .agents/{name}/agent.md             # root-page agent definition
  {page}/.agents/{name}/agent.md      # child-page agent definition
  .skills/{name}/skill.md             # skill instructions (site-scoped)
  .skills/{name}/mcp.json             # MCP server config for that skill
```

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
- `.agents/{name}/agent.md`
- `{page}/.agents/{name}/agent.md`

This is validated on both `PUT /content` and `POST /chat`.

### Agent framework (Strands Agents)
All agents inherit from `BaseAgent` (`backend/agents/base.py`), which itself inherits from `strands.Agent`. Each class has a `SYSTEM_PROMPT` class variable (a Python format-string; placeholders are filled via `**prompt_vars` in the constructor).

The model is `AnthropicModel` from `strands.models.anthropic`, defaulting to `DEFAULT_MODEL = "claude-opus-4-6"` (see `base.py`). The model can be overridden at runtime via the `AGENTSCRIBE_MODEL` env var.

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

## Environment Variables

| Variable | Where | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | backend | Required for Claude API (used by strands AnthropicModel) |
| `S3_BUCKET` | backend | S3 bucket name |
| `ALLOWED_ORIGINS` | backend | Comma-separated CORS origins |
| `AWS_PROFILE` | backend | Optional named AWS profile |
| `AGENTSCRIBE_MODEL` | backend | Override default Claude model ID |
| `SQS_QUEUE_URL` | backend | SQS queue URL for async agent execution (RunnerAgent) |
| `VITE_API_BASE` | frontend build | ALB URL for production |
| `VITE_SITE` | frontend dev | Override site name in dev |

Copy `backend/.env.example` to `backend/.env` for local development.
