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
