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
AWS_PROFILE=myprofile AWS_REGION=us-east-1 ./infra/scripts/setup_dynamodb.sh
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
./infra/scripts/setup_dynamodb.sh
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

## 2. Cognito app clients

Create **two** Cognito app clients on your User Pool:

| Client | Type | Used by |
|---|---|---|
| `yoloscribe-backend` | Confidential (has client secret) | Backend — MCP OAuth PKCE, token exchange |
| `yoloscribe-frontend` | Public (no secret, PKCE only) | Browser — user sign-in via Hosted UI |

Register the following callback URLs on each client:
- `https://<your-domain>/` — frontend redirect after sign-in
- `https://<your-domain>/mcp/oauth/callback/*` — backend MCP OAuth (confidential client only)

## 3. Deploy with Helm

The `infra/helm/yoloscribe-backend` chart supports `authProvider: cognito`. Create a values file for your environment (e.g. `backend.prod.values.yaml`):

```yaml
image:
  repository: ghcr.io/<your-org>/yoloscribe-backend
  tag: latest

config:
  authProvider: cognito
  awsRegion: us-east-1
  s3Bucket: yoloscribe-prod
  sqsQueueUrl: https://sqs.us-east-1.amazonaws.com/<account>/yoloscribe-prod-jobs
  eksOidcProvider: oidc.eks.us-east-1.amazonaws.com/id/<cluster-id>
  awsAccountId: "<account-id>"
  k8sNamespace: yoloscribe
  cloudfrontDomain: <your-cloudfront-domain>
  mcpBaseUrl: https://<your-domain>
  allowedOrigins: "https://<your-domain>"

cognito:
  userPoolId: us-east-1_XXXXXXXXX
  clientId: <confidential-app-client-id>    # backend client
  domain: https://your-pool.auth.us-east-1.amazoncognito.com

ingress:
  enabled: true
  host: <your-domain>
  certificateArn: arn:aws:acm:us-east-1:<account>:certificate/<cert-id>

serviceAccount:
  iamRoleArn: arn:aws:iam::<account>:role/yoloscribe-backend
```

Then deploy:

```bash
helm upgrade --install yoloscribe-backend infra/helm/yoloscribe-backend \
  -f backend.prod.values.yaml \
  --set cognitoClientSecret=<confidential-client-secret> \
  --set webhookSecret=<webhook-secret> \
  --set anthropicApiKey=sk-ant-... \
  --namespace yoloscribe --create-namespace
```

**IAM permissions required by the backend service account (IRSA):**
- `cognito-idp:AdminDeleteUser` on your User Pool
- `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`, `Query` on both DynamoDB tables
- `s3:GetObject`, `PutObject`, `DeleteObject`, `ListBucket` on your S3 bucket
- `secretsmanager:CreateSecret`, `PutSecretValue`, `GetSecretValue`, `DescribeSecret` on `yoloscribe*` secrets
- `iam:CreateRole`, `PutRolePolicy`, `GetRole` on `yoloscribe-user-*` roles
- `sqs:SendMessage` on your SQS queues

## 4. Set up CloudFront media delivery (optional)

Skip this step if you do not need video or audio support. Images are always served through the backend and require no additional setup.

Run the one-time setup script to create a CloudFront key pair, register it as a trusted signer on your distribution's `*/assets/*` cache behaviour, and store the private key in Secrets Manager:

```bash
AWS_PROFILE=myprofile AWS_REGION=us-east-1 \
DISTRIBUTION_ID=E1EXAMPLE \
./infra/scripts/setup_cloudfront_media.sh
```

The script prints the `cloudfrontSigningKeyId` value to add to your Helm values file. Then re-deploy with:

```bash
helm upgrade yoloscribe-backend infra/helm/yoloscribe-backend \
  -f backend.prod.values.yaml \
  --set config.cloudfrontSigningKeyId=<key-pair-id> \
  --set config.cloudfrontMediaDomain=<your-cloudfront-domain> \
  ...
```

The `*/assets/*` cache behaviour on your CloudFront distribution must have:
- **Trusted key groups:** the group created by the script (`yoloscribe-media-group`)
- **Cache policy:** `CachingDisabled` (signed cookies are per-user; caching would leak content across users)
- **Viewer protocol policy:** HTTPS only

## 5. Build and deploy the frontend

Build the frontend with Cognito env vars:

```bash
cd frontend
VITE_AUTH_PROVIDER=cognito \
VITE_COGNITO_CLIENT_ID=<public-app-client-id> \
VITE_COGNITO_DOMAIN=https://your-pool.auth.us-east-1.amazoncognito.com \
VITE_API_BASE=https://<your-domain>/api \
VITE_CLOUDFRONT_MEDIA_DOMAIN=<your-cloudfront-domain> \
npm run build
```

Omit `VITE_CLOUDFRONT_MEDIA_DOMAIN` if you skipped step 4 (no video/audio support). Images will still work.

Upload `dist/` to your S3 bucket / CloudFront origin. The public Cognito app client handles browser sign-in via PKCE — no client secret required in the frontend bundle.

## 5. Configure environment variables (non-Helm)

If deploying without Helm (e.g. ECS, App Runner), set these on the backend container:

```
AUTH_PROVIDER=cognito

COGNITO_USER_POOL_ID=us-east-1_XXXXXXXXX
COGNITO_CLIENT_ID=<confidential-app-client-id>
COGNITO_CLIENT_SECRET=<confidential-app-client-secret>
COGNITO_DOMAIN=https://your-pool.auth.us-east-1.amazoncognito.com
AWS_REGION=us-east-1

# Optional — only if you renamed the tables in step 1
DYNAMODB_USER_SITE_TABLE=yoloscribe-user-site
DYNAMODB_API_TOKENS_TABLE=yoloscribe-api-tokens
```

**IAM permissions required by the backend pod/task:**
- `cognito-idp:AdminDeleteUser` on your User Pool
- `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`, `Query` on both DynamoDB tables

## 6. Post-confirmation Lambda trigger (user provisioning)

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

## 7. MCP server connection

For Cognito operators, skip the MCP OAuth PKCE flow and connect Claude Code directly with a YoloScribe API token:

1. Sign in to YoloScribe and generate an API token from **Settings → API Tokens**.
2. Add the MCP server to Claude Code:

```bash
claude mcp add --transport http yoloscribe https://<your-domain>/mcp/v1/ \
  --header "Authorization: Bearer as_<your-token>"
```

API tokens (`as_` prefix) are accepted by the MCP auth middleware and are fully scoped to your site. The OAuth PKCE flow (`/mcp/oauth/...`) is available for Supabase operators only.
