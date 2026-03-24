# Local Development — Quick Start

No AWS account required. Everything runs locally via Docker Compose.

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker + Docker Compose)
- [Node.js](https://nodejs.org/) 18+ (for the frontend)
- An [Anthropic API key](https://console.anthropic.com/)

## 1. Configure

```bash
cp .env.local .env
```

Open `.env` and set your API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

## 2. Start everything

```bash
docker compose up -d
```

This starts:
| Service | URL | Description |
|---|---|---|
| **frontend** | http://localhost:5173 | React SPA (nginx, built) → redirects to `/local/` |
| **backend** | http://localhost:8000 | FastAPI backend |
| **agent-runner** | — | Async agent worker (polls SQS) |
| **MinIO** | http://localhost:9000 | Local S3 (API) |
| MinIO console | http://localhost:9001 | Web UI — user: `yoloscribe` / pass: `yoloscribe` |
| **ElasticMQ** | http://localhost:9324 | Local SQS |

## 3. Open the browser

Navigate to http://localhost:5173 — it redirects automatically to your local wiki at `/local/`.

No sign-in required. The topbar shows **Local** instead of an auth avatar.

## How local mode works

Setting `LOCAL_MODE=true` makes the following changes:

- **Auth** — all requests are treated as the `local` user with site `local`. No sign-in required.
- **Provisioning** — `/provision` skips Supabase and IAM/K8s calls; it just writes files to S3 (MinIO).
- **S3** — all bucket operations go to MinIO via `S3_ENDPOINT_URL`.
- **SQS** — all queue operations go to ElasticMQ via `SQS_ENDPOINT_URL`.
- **Agent indexing** — `LOCAL_RUNNER=true` runs the index job inline (no K8s).

## Active frontend development (optional)

The frontend container serves a pre-built bundle — there's no hot reload. If you're working on the frontend, run the Vite dev server on the host instead:

```bash
# Start everything except the frontend container
docker compose up -d minio minio-init elasticmq backend agent-runner

# Run the Vite dev server with local mode flags
cd frontend
npm install
VITE_LOCAL_MODE=true npm run dev
```

The Vite proxy (`/api → localhost:8000`) routes API calls to the backend container. Open http://localhost:5173/local/.

After frontend changes, rebuild the container with:

```bash
docker compose build frontend && docker compose up -d frontend
```

## Running backend outside Docker (optional)

If you want to iterate on the backend without rebuilding the image:

```bash
# Start only the infrastructure services
docker compose up -d minio minio-init elasticmq

# Run the backend locally
cd backend
uv sync
uv run --env-file ../.env uvicorn main:app --reload
```

## Limitations

The following features require AWS in production and are disabled / degraded in local mode:

- **OAuth tool credentials** — Secrets Manager is not available; saving credentials will fail.
- **Semantic search** — requires S3 Vectors + Bedrock; not available locally.
- **User provisioning** — Supabase row is not created; only the S3 content is written.

---

# AWS Self-Hosted Install (Cognito + DynamoDB)

This section covers replacing Supabase with an all-AWS auth stack. Use this path if you are self-hosting YoloScribe and do not want a Supabase dependency.

**Prerequisites:**
- An AWS account with an existing Cognito User Pool configured for your identity provider (Google, Okta, SAML, etc.)
- The Cognito Hosted UI enabled on your User Pool
- An app client with a client secret, and the YoloScribe callback URL registered as an allowed redirect URI

## 1. Create DynamoDB tables

Run the setup script once before deploying:

```bash
AWS_PROFILE=myprofile AWS_REGION=us-east-1 ./scripts/setup_dynamodb.sh
```

This creates two tables (idempotent — safe to re-run):

| Table | Purpose |
|---|---|
| `yoloscribe-user-site` | Maps user UUID → site name |
| `yoloscribe-api-tokens` | Stores hashed API tokens |

Override table names with env vars if needed:

```bash
DYNAMODB_USER_SITE_TABLE=my-user-site \
DYNAMODB_API_TOKENS_TABLE=my-api-tokens \
AWS_REGION=eu-west-1 \
./scripts/setup_dynamodb.sh
```

**Table schemas:**

`yoloscribe-user-site`:
- PK: `user_id` (S) — Cognito sub (UUID)
- Attributes: `site_name` (S), `theme` (S)

`yoloscribe-api-tokens`:
- PK: `token_id` (S) — UUID
- GSI `user_id-index`: PK `user_id` (S), SK `created_at` (S) — for listing a user's tokens
- GSI `token_hash-index`: PK `token_hash` (S) — for auth-time lookup
- Attributes: `user_id`, `site_name`, `name`, `token_hash`, `created_at`, `expires_at`, `last_used_at`, `revoked_at`

## 2. Configure environment variables

Set these in your Helm values or `.env` file:

```
AUTH_PROVIDER=cognito

COGNITO_USER_POOL_ID=us-east-1_XXXXXXXXX
COGNITO_CLIENT_ID=<your-app-client-id>
COGNITO_CLIENT_SECRET=<your-app-client-secret>
COGNITO_DOMAIN=https://your-domain.auth.us-east-1.amazoncognito.com
AWS_REGION=us-east-1

# Optional — only if you renamed the tables
DYNAMODB_USER_SITE_TABLE=yoloscribe-user-site
DYNAMODB_API_TOKENS_TABLE=yoloscribe-api-tokens
```

**IAM permissions required by the backend pod/task:**
- `cognito-idp:AdminDeleteUser` on your User Pool
- `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`, `Query` on both DynamoDB tables

## 3. Post-confirmation Lambda trigger (user provisioning)

With Supabase, a webhook fires after sign-up. With Cognito, the equivalent is a **post-confirmation Lambda trigger**.

YoloScribe does not ship the Lambda function. Configure your own trigger pointing at the existing `/webhooks/user-created` endpoint:

1. In the Cognito console, go to your User Pool → **User pool properties** → **Add Lambda trigger** → **Post confirmation**.
2. Create a Lambda function that sends a POST to `https://<your-domain>/webhooks/user-created` with:
   ```json
   {
     "user_id": "<cognito-sub>",
     "email": "<user-email>"
   }
   ```
   Include the `X-Webhook-Secret: <WEBHOOK_SECRET>` header matching your backend's `WEBHOOK_SECRET` env var.
3. Grant the Lambda function internet access (via a VPC NAT gateway or a public subnet).

This webhook triggers IAM/K8s infrastructure provisioning for the new user — the same path as Supabase.

## 4. MCP server connection

For Cognito operators, skip the MCP OAuth PKCE flow and connect Claude Code directly with a YoloScribe API token:

1. Sign in to YoloScribe and generate an API token from **Settings → API Tokens**.
2. Add the MCP server to Claude Code:

```bash
claude mcp add --transport http yoloscribe https://<your-domain>/mcp/v1 \
  --header "Authorization: Bearer as_<your-token>"
```

API tokens (`as_` prefix) are accepted by the MCP auth middleware and are fully scoped to your site. The OAuth PKCE flow (`/mcp/oauth/...`) is available for Supabase operators only.
