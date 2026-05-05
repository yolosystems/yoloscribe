# YoloScribe

**An AI-powered wiki where every page can have its own agents.**

YoloScribe is a self-hostable wiki built on [Strands Agents](https://strandsagents.com) and the [Model Context Protocol (MCP)](https://modelcontextprotocol.io). Pages are Markdown files stored in S3. Each page can define agents that read and write content — triggered from the chat interface, the MCP server, Discord, or Obsidian. A built-in remote MCP server lets Claude Code and other AI tools interact with your wiki directly.

---

## Quick start — 5 minutes, no AWS required

The full stack runs locally via Docker Compose using MinIO (S3) and ElasticMQ (SQS) in place of AWS services.

**Prerequisites:** [Docker Desktop](https://www.docker.com/products/docker-desktop/), an [Anthropic API key](https://console.anthropic.com/)

```bash
git clone https://github.com/nate-yolodev/yoloscribe.git
cd yoloscribe
cp .env.local .env
# Open .env and set ANTHROPIC_API_KEY=sk-ant-...
docker compose up -d
```

Open http://localhost:5173 — it redirects to your local wiki. No sign-in required.

| Service | URL |
|---|---|
| Wiki | http://localhost:5173 |
| Backend API + Swagger | http://localhost:8000/docs |
| MinIO console | http://localhost:9001 (user: `yoloscribe` / pass: `yoloscribe`) |

See [INSTALL.md](INSTALL.md) for active frontend development, running services outside Docker, and full self-hosted AWS deployment instructions.

---

## Features

- **Markdown wiki** — pages stored as `content.md` in S3; full version history via S3 versioning
- **Per-page AI agents** — define agents in `agent.md`; they run asynchronously via SQS
- **Skills** — connect agents to external tools (GitHub, Linear, Google Workspace, etc.) via MCP servers
- **Remote MCP server** — expose your wiki as an MCP server for Claude Code and other agents
- **Semantic search** — Bedrock embeddings + S3 Vectors for context-aware retrieval
- **Access control** — public / private / shared-with pages; Supabase JWT auth
- **Multi-model routing** — route agents to Haiku, Sonnet, or Opus (Anthropic direct or Bedrock)
- **Discord integration** — post messages in any channel to update your wiki
- **Obsidian integration** — two-way sync between your vault and YoloScribe pages

---

## Integrations

### Claude Code / MCP

YoloScribe exposes a remote MCP server at `/mcp/v1`. Connect Claude Code with a single command:

```bash
claude mcp add --transport http yoloscribe https://your-domain/mcp/v1/
```

Claude Code discovers the OAuth endpoints automatically and prompts you to sign in with Google. No token copy-pasting required. (Cognito operators can use a YoloScribe API token instead — see [INSTALL.md](INSTALL.md#7-mcp-server-connection).)

**Available tools:** `wiki_create`, `wiki_read`, `wiki_update`, `wiki_delete`, `wiki_list`, `search_wiki`, `search_semantic`, `agent_create`, `agent_get_status`, `agent_update_context`, `agent_get_context`, `agent_list`

### Discord

The Discord bot lets any channel message trigger a wiki update. Set it up once with a slash command:

```
/yoloscribe setup <your-api-token>
```

After that, any message in the configured channel is routed to your wiki's chat endpoint. Target a specific page by prefixing your message:

```
[/projects/roadmap] Update the Q3 section with the new launch date
```

Messages without a prefix update the root page. The bot reacts with ⏳ on receipt and swaps to ✅ or ❌ on completion.

The bot lives in `discord-bot/` and is deployed as a standalone Docker container.

### Obsidian

The Obsidian plugin syncs your vault with YoloScribe pages bidirectionally. Install it from `obsidian-plugin/`, add your API token in plugin settings, and it bootstraps a full sync on load. Subsequent saves are pushed automatically. A live status bar indicator shows the SSE connection state (`YS ○` / `YS ●`).

Page paths map directly to vault paths — `projects/roadmap` in YoloScribe becomes `projects/roadmap.md` in your vault.

---

## Auth & SSO

YoloScribe supports two auth providers:

### Supabase (default)

The default setup uses [Supabase](https://supabase.com) for auth (free tier works). Users sign in with Google OAuth via the Supabase Hosted UI. After sign-up, a webhook fires to provision the user's infrastructure (IAM role, Kubernetes ServiceAccount, Secrets Manager namespace).

Required env vars: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`

The MCP server authenticates via OAuth PKCE — Claude Code triggers the browser flow automatically.

### Cognito (self-hosted AWS alternative)

For operators who want a fully AWS-native stack without a Supabase dependency, YoloScribe supports AWS Cognito as an auth provider. This path uses DynamoDB for user/site mapping and API tokens instead of JWTs for MCP auth.

Set `AUTH_PROVIDER=cognito` on the backend and follow the Helm-based setup in [INSTALL.md](INSTALL.md#aws-self-hosted-install-cognito--dynamodb).

---

## Agent security model

In production, each user's agents run with a dedicated IAM role scoped strictly to that user's data. No agent can access another user's wiki content or credentials.

### Per-user IAM roles

When a user signs up, the backend provisions a role named `yoloscribe/yoloscribe-user-{user_id}`. The inline policy follows the principle of least privilege — it only allows access to that user's S3 prefix and Secrets Manager namespace:

```json
{
  "Statement": [
    {
      "Sid": "S3ReadWriteUserPrefix",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::{BUCKET}/{USER_ID}/*"
    },
    {
      "Sid": "S3ListUserPrefix",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::{BUCKET}",
      "Condition": { "StringLike": { "s3:prefix": "{USER_ID}/*" } }
    },
    {
      "Sid": "SecretsManagerReadUserSecrets",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:*:*:secret:yoloscribe/{USER_ID}/*"
    }
  ]
}
```

### IRSA (IAM Roles for Service Accounts)

The agent-runner pod on EKS assumes the user's IAM role at execution time via [IRSA](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html). Each agent job runs in a Kubernetes Pod with a ServiceAccount annotated with the user's role ARN. AWS's STS token projection grants temporary credentials — no long-lived keys are stored or passed to agents.

The agent-runner's own role (`yoloscribe-agent-runner`) can only poll the SQS queue and read agent/skill definitions from S3. It cannot access any user data directly. User data access only becomes possible after assuming the narrowly-scoped per-user role.

### Credential isolation for skills

When an agent uses a skill (e.g. GitHub, Linear), its OAuth tokens are stored in Secrets Manager under `yoloscribe/{user_id}/{skill_name}`. The per-user IAM policy's Secrets Manager statement is scoped to that exact prefix, so one user's skill credentials are never readable by another user's agent.

---

## Project structure

```
frontend/        React SPA (Vite + TypeScript) — served from S3 + CloudFront
backend/         FastAPI — wiki API, chat endpoint, remote MCP server at /mcp/v1
agent-runner/    SQS worker — executes user-defined agents asynchronously
indexer/         SQS worker — indexes content into S3 Vectors for semantic search
discord-bot/     Discord bot — routes channel messages to the wiki chat API
obsidian-plugin/ Obsidian plugin — bidirectional vault ↔ wiki sync
libs/            Shared Python libraries (mcp_oauth, etc.)
infra/           Helm charts, IAM policies, deploy scripts
default-skills/  Built-in skill definitions (GitHub, Linear, Google Workspace, etc.)
specs/           Feature specifications
```

**Data model:** every site is an S3 prefix. Pages live at `{site}/{page}/content.md`. Agent definitions at `{site}/{page}/.agents/{name}/agent.md`. Skills at `{site}/.skills/{name}/`.

---

## Self-hosting

### Local (Docker Compose)

See the [Quick start](#quick-start--5-minutes-no-aws-required) section above, or [INSTALL.md](INSTALL.md) for full details including frontend hot-reload and running backend services outside Docker.

### AWS / EKS

Production Helm charts are in `infra/helm/`. The backend, agent-runner, and indexer each have a Dockerfile and a corresponding chart. Deployment uses IRSA for credential isolation — each service runs with a least-privilege IAM role.

Full EKS + Helm deployment instructions, including Cognito setup and CloudFront media delivery, are in [INSTALL.md](INSTALL.md).

---

## Configuration

Copy `env.example` to `.env`. Key variables:

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `S3_BUCKET` | Yes | S3 bucket for wiki content |
| `SUPABASE_URL` | Supabase auth | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase auth | Supabase service role key |
| `VITE_SUPABASE_URL` | Supabase auth | Supabase URL for browser client |
| `VITE_SUPABASE_ANON_KEY` | Supabase auth | Supabase anon key for browser client |
| `SQS_QUEUE_URL` | For agents | SQS queue for async agent execution |
| `SQS_INDEXING_QUEUE_URL` | For search | SQS queue for indexing jobs |
| `S3_VECTORS_BUCKET` | For semantic search | S3 Vectors bucket |
| `YOLOSCRIBE_MODEL` | No | Global model key (`haiku`\|`sonnet`\|`opus`\|`bedrock-*`), default `sonnet` |
| `LOCAL_MODE` | Local dev | Set `true` to bypass Supabase auth and AWS services |

See `env.example` for the full list including per-agent model overrides and CloudFront media delivery.

---

## Agent definition format

Create an `agent.md` in any page's `.agents/` directory (or via the UI):

```markdown
# Agent: changelog-updater

## Description

Keeps the project changelog up to date whenever a pull request is merged.

## Model

sonnet

## Skills

- github
```

Agents run asynchronously when triggered via the chat interface, MCP server, Discord, or Obsidian. The `## Skills` section lists which MCP-backed tools the agent has access to.

---

## License

[MIT](LICENSE)
