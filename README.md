# Yolo Scribe

**An AI-powered wiki where every page can be edited by an agent.**

Yolo Scribe is a self-hostable wiki built around [Strands Agents](https://strandsagents.com) and the [Model Context Protocol (MCP)](https://modelcontextprotocol.io). Pages are markdown files stored in S3. Each page can have its own AI agents that read and write content. A built-in remote MCP server lets Claude Code and other AI coding tools read and write your wiki directly.

---

## Features

- **Markdown wiki** — pages stored as `content.md` in S3; full version history via S3 versioning
- **Per-page AI agents** — define agents in `agent.md`; they run asynchronously via SQS
- **Skills** — connect agents to external tools (GitHub, Linear, Google Workspace, etc.) via MCP
- **Remote MCP server** — expose your wiki as an MCP server for Claude Code and other agents
- **Semantic search** — Bedrock embeddings + S3 Vectors for context-aware retrieval
- **Access control** — public / private / shared-with pages; Supabase JWT auth
- **Multi-model routing** — route agents to Haiku, Sonnet, or Opus (Anthropic direct or Bedrock)

---

## Architecture

```
frontend/        React SPA (Vite) — served from S3 + CloudFront
backend/         FastAPI — wiki API, chat endpoint, remote MCP server at /mcp/v1
agent-runner/    SQS worker — executes user-defined agents asynchronously
indexer/         SQS worker — indexes content into S3 Vectors for semantic search
libs/            Shared Python libraries (mcp_oauth, etc.)
infra/           Helm charts, IAM scripts, deploy scripts
```

**Data model:** every "site" is an S3 prefix. Pages live at `{site}/{page}/content.md`. Agent definitions at `{site}/.agents/{name}/agent.md`. Skills at `{site}/.skills/{name}/`.

---

## Prerequisites

- [Supabase](https://supabase.com) project (free tier works) — auth + user/site mapping
- AWS account — S3 bucket, SQS queues, Bedrock access (us-west-2 recommended)
- [uv](https://docs.astral.sh/uv/) — Python package manager
- Node.js 20+

---

## Local Development

### 1. Clone and configure

```bash
git clone https://github.com/nate-yolodev/yoloscribe.git
cd yoloscribe
cp env.example .env
# Edit .env — fill in ANTHROPIC_API_KEY, S3_BUCKET, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
# VITE_SUPABASE_URL, VITE_SUPABASE_ANON_KEY at minimum
```

### 2. Backend

```bash
cd backend
uv sync
uv run --env-file ../.env uvicorn main:app --reload
# API available at http://localhost:8000
# Swagger UI at http://localhost:8000/docs
```

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
# Vite dev server at http://localhost:5173
# Proxies /api → http://localhost:8000
```

### 4. Agent runner (optional — needed for async agent execution)

```bash
cd agent-runner
uv sync
uv run --env-file ../.env python -m agent_runner
```

### 5. Indexer (optional — needed for semantic search)

```bash
cd indexer
uv sync
uv run --env-file ../.env python -m indexer
```

---

## Self-Hosting

### Docker

Each service has a Dockerfile. A minimal production stack:

```bash
# Backend
docker build -f backend/Dockerfile -t yoloscribe-backend .
docker run -p 8000:8000 --env-file .env yoloscribe-backend

# Agent runner
docker build -f agent-runner/Dockerfile -t yoloscribe-agent-runner .
docker run --env-file .env yoloscribe-agent-runner

# Indexer
docker build -f indexer/Dockerfile -t yoloscribe-indexer .
docker run --env-file .env yoloscribe-indexer
```

The frontend is a static SPA — build it and serve from S3/CloudFront, Nginx, or any CDN:

```bash
cd frontend
npm run build   # outputs to dist/
```

### AWS / Kubernetes (EKS)

Production Helm charts and IAM setup scripts are in `infra/`. Detailed EKS deployment documentation is coming soon.

---

## MCP Server

Yolo Scribe exposes a remote MCP server at `/mcp/v1` for use with Claude Code and other MCP-compatible AI agents.

**Connect with Claude Code:**

```bash
claude mcp add --transport http yoloscribe https://your-domain/mcp/v1
```

Claude Code will discover the OAuth endpoints automatically and prompt you to sign in with Google. No token copy-pasting required.

**Available tools:** `wiki_create`, `wiki_read`, `wiki_update`, `wiki_delete`, `wiki_list`, `search_wiki`, `search_semantic`, `agent_create`, `agent_get_status`, `agent_update_context`, `agent_get_context`, `agent_list`.

---

## Configuration

Copy `env.example` to `.env`. Key variables:

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `S3_BUCKET` | Yes | S3 bucket for wiki content |
| `SUPABASE_URL` | Yes | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | Supabase service role key |
| `VITE_SUPABASE_URL` | Yes (frontend) | Supabase URL for browser client |
| `VITE_SUPABASE_ANON_KEY` | Yes (frontend) | Supabase anon key for browser client |
| `SQS_QUEUE_URL` | For agents | SQS queue for async agent execution |
| `SQS_INDEXING_QUEUE_URL` | For search | SQS queue for indexing jobs |
| `S3_VECTORS_BUCKET` | For semantic search | S3 Vectors bucket |
| `AGENTSCRIBE_MODEL` | No | Global model key (`haiku`\|`sonnet`\|`opus`\|`bedrock-*`), default `sonnet` |

See `env.example` for the full list.

---

## Agent Definition Format

Create agents by adding `agent.md` files to any page's `.agents/` directory:

```markdown
# Agent: my-agent

## Description

Keeps the project changelog up to date whenever a pull request is merged.

## Model

sonnet

## Skills

- github
```

Agents run asynchronously when triggered via the chat interface or the MCP server.

---

## License

[GNU Affero General Public License v3.0](LICENSE) — if you run a modified version as a service, you must open source your changes.

