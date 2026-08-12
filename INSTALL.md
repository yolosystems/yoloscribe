# Installation

## Local development (no AWS required)

The full stack runs via Docker Compose using MinIO (S3) and ElasticMQ (SQS) in place of AWS services. No sign-in required.

**Prerequisites:** [Docker Desktop](https://www.docker.com/products/docker-desktop/), an [Anthropic API key](https://console.anthropic.com/)

```bash
cp .env.local .env
# Set ANTHROPIC_API_KEY in .env
docker compose up -d
```

Open http://localhost:5173 — redirects to `/local/`.

| Service | URL |
|---|---|
| Wiki | http://localhost:5173 |
| Backend + Swagger | http://localhost:8000/docs |
| MinIO console | http://localhost:9001 (user/pass: `yoloscribe`) |
| ElasticMQ | http://localhost:9324 |

### Active frontend development

The frontend container serves a pre-built bundle. For hot reload, run the Vite dev server on the host instead:

```bash
docker compose up -d minio minio-init elasticmq backend agent-runner
cd frontend && npm install && VITE_LOCAL_MODE=true npm run dev
```

### Running backend outside Docker

```bash
docker compose up -d minio minio-init elasticmq
cd backend && uv sync && uv run --env-file ../.env uvicorn main:app --reload
```

### Limitations in local mode

- **Semantic search** — requires S3 Vectors + Bedrock; not available locally
- **Single user only** — local mode runs as a fixed `local` user with no auth; multi-user provisioning (Supabase/Cognito rows, per-user IAM roles) is not needed and not supported

OAuth-based skill credentials work in local mode — tokens are stored in MinIO at `_secrets/` instead of Secrets Manager.

---

## Production install

### Third-party services

#### Anthropic API

Create an API key at [console.anthropic.com](https://console.anthropic.com). Set as `ANTHROPIC_API_KEY`. Required — all agent execution goes through the Claude API (or Bedrock; see AWS section below).

#### Supabase (default auth)

YoloScribe uses [Supabase](https://supabase.com) for auth by default. Free tier is sufficient.

- Create a project and enable **Google OAuth** under Authentication → Providers
- Note your **Project URL** (`SUPABASE_URL`) and **service role key** (`SUPABASE_SERVICE_ROLE_KEY`)
- Note your **anon key** (`VITE_SUPABASE_ANON_KEY`) for the frontend build

Per-user infrastructure (site, IAM role, Kubernetes ServiceAccount, Secrets Manager placeholder) is provisioned on first sign-in through the onboarding flow: the frontend calls the authenticated `POST /provision` after the user picks a site name.

#### Cognito (alternative — all-AWS, no Supabase)

If you prefer a fully AWS-native stack, Cognito can replace Supabase. Set `AUTH_PROVIDER=cognito` on the backend.

- Create a **User Pool** with your identity provider (Google, Okta, SAML, etc.) and the Hosted UI enabled
- Create a single **public, PKCE-only app client** for the browser (no client secret — a browser cannot hold one securely, and the backend only validates issued JWTs)
- Register `https://your-domain/` and `https://your-domain/mcp/oauth/callback/*` as allowed redirect URIs on that client

Set `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `COGNITO_DOMAIN` on the backend. Create the two DynamoDB tables (see below). Per-user infrastructure is provisioned on first sign-in via the authenticated `POST /provision` onboarding flow (same as the Supabase path).

#### Messaging bot (optional)

- Create a Discord application at [discord.com/developers](https://discord.com/developers/applications)
- Create a bot user, enable the **Message Content** privileged intent, and copy the bot token (`DISCORD_BOT_TOKEN`)
- Generate a 32-byte AES key for encrypting per-server API tokens: `node -e "console.log(require('crypto').randomBytes(32).toString('base64'))"` → set as `MESSAGING_AES_KEY`
- Set `YOLOSCRIBE_API_URL` to your backend's public URL, and `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` to your Supabase project's values
- Set `ENABLED_ADAPTERS` to the comma-separated list of platform adapters to enable (currently `discord`)

The bot is deployed as a standalone container from `messaging-bot/Dockerfile`.

---

### AWS infrastructure

Copy `env.example` to `.env` and fill in values as you create each resource.

#### S3 — wiki content

Create one S3 bucket for wiki content. Enable **versioning** (provides page history). Set as `S3_BUCKET`.

The bucket does not need to be public. The backend accesses it via IAM role; the frontend never talks to S3 directly.

#### S3 Vectors — semantic search

Create an **S3 Vectors bucket** and an index within it (1024 dimensions, cosine similarity, for use with `amazon.titan-embed-text-v2`). Set as `S3_VECTORS_BUCKET` and `S3_VECTORS_INDEX_NAME`.

This is only required for semantic search. If you skip it, keyword search still works.

#### SQS — async job queues

Create two **standard SQS queues**:

| Queue | Env var | Purpose |
|---|---|---|
| `yoloscribe-runner` | `SQS_QUEUE_URL` | Agent execution jobs |
| `yoloscribe-indexing` | `SQS_INDEXING_QUEUE_URL` | Search indexing jobs |

#### Bedrock — embeddings and models (optional)

Enable model access in the Bedrock console for your region:

- **`amazon.titan-embed-text-v2:0`** — required for semantic search
- **`anthropic.claude-*`** — only needed if you want to route agents through Bedrock instead of the Anthropic API directly (set `YOLOSCRIBE_MODEL=bedrock-sonnet` etc.)

`us-west-2` has the broadest model availability.

#### IAM — service roles

Create three IAM roles with IRSA trust policies (trust the EKS OIDC provider for the appropriate Kubernetes namespace/ServiceAccount). Attach the policies from `infra/iam/`:

| Role | Policy file | Used by |
|---|---|---|
| `yoloscribe-backend` | `yoloscribe-backend-policy.json` | Backend pod — S3 (incl. object versions), DynamoDB, SQS, Secrets Manager, IAM (to provision user roles), Bedrock |
| `yoloscribe-agent-runner` | `yoloscribe-agent-runner-policy.json` | Agent-runner pod — SQS poll, S3 read (agent/skill definitions only) |
| `yoloscribe-indexer` | `yoloscribe-indexer-policy.json` | Indexer pod — SQS poll, S3 read, Bedrock, S3 Vectors |

**Bedrock: inference vs. embeddings.** These two paths have different requirements, and conflating them is the usual source of IAM surprises here.

- **Inference** (all agent and chat model calls) goes through the **LiteLLM proxy** since YOL-512. Only the LiteLLM pod's role needs model-invocation permissions — see `infra/helm/litellm.<env>.values.yaml`. The YoloScribe roles do **not** need a Bedrock inference policy attached, including `AmazonBedrockMantleInferenceAccess`.
- **Embeddings** deliberately bypass LiteLLM. The backend, agent-runner, and indexer each call `bedrock-runtime:InvokeModel` directly against `amazon.titan-embed-text-v2:0` for semantic search. These roles need a `bedrock:InvokeModel` grant scoped to the embedding model — see the `BedrockEmbed` statement in each policy file.

> **If you are removing a previously attached Bedrock managed policy from these roles, check the inline policy first.** A role whose inline policy has no `BedrockEmbed` statement may be relying on the broader managed policy for its embedding calls, and detaching it will cause semantic search to start returning 403s with no other symptom.

Per-user roles (`yoloscribe/yoloscribe-user-{user_id}`) are provisioned automatically at sign-up by the backend using the template in `infra/iam/yoloscribe-user-policy-template.json`. Each role is scoped to that user's S3 prefix and Secrets Manager namespace only.

Set `EKS_OIDC_PROVIDER`, `AWS_ACCOUNT_ID`, `AWS_REGION`, and `K8S_NAMESPACE` so the backend can construct correct role ARNs and trust policies at provision time.

#### EKS — container orchestration

Create an EKS cluster with the **OIDC provider** enabled (required for IRSA). The backend, agent-runner, and indexer each run as a Deployment in the same namespace (default: `yoloscribe`).

Annotate each Kubernetes ServiceAccount with its IAM role ARN:

```yaml
annotations:
  eks.amazonaws.com/role-arn: arn:aws:iam::<account>:role/<role-name>
```

The Helm charts in `infra/helm/` handle this automatically when you set `serviceAccount.iamRoleArn` in the values file.

#### Cluster add-ons (prerequisites)

YoloScribe assumes a few standard EKS cluster add-ons are already installed. These are cluster-wide, installed once by whoever operates the cluster, and are **not** vendored by YoloScribe — install each from its upstream project. Anyone running EKS in production will typically have these already.

| Add-on | Why YoloScribe needs it |
|---|---|
| [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/) | Provisions the ALB for each `Ingress` (backend, and the LiteLLM proxy). Required for the `className: alb` ingresses and for the WAF association annotation (`infra/waf/`). |
| [ExternalDNS](https://kubernetes-sigs.github.io/external-dns/) | Creates the Route 53 records for ingress hostnames. EKS gives you no DNS automation out of the box. |
| [EBS CSI driver](https://github.com/kubernetes-sigs/aws-ebs-csi-driver) | Dynamic `PersistentVolume` provisioning for any stateful dependency you self-host in-cluster (e.g. Postgres, Phoenix). |
| [External Secrets Operator (ESO)](https://external-secrets.io/) | Syncs AWS Secrets Manager → Kubernetes `Secret`s. Point a chart's `existingSecret` value at an ESO-materialized secret to keep plaintext out of Helm `--set` / release values (recommended over injecting secrets at install time). |

> **EKS Auto Mode note:** Auto Mode ships its own load-balancing and requires `IngressClass` + `IngressClassParams` rather than the legacy ALB controller annotations, and still has no built-in Route 53 integration — install ExternalDNS separately.

#### Secrets Manager

No manual setup required. The backend creates per-user secret prefixes (`yoloscribe/{user_id}/`) automatically when users connect skills (GitHub, Linear, etc.). The backend IAM role needs `secretsmanager:CreateSecret`, `PutSecretValue`, `GetSecretValue`, `DescribeSecret` on `yoloscribe*` resources.

#### DynamoDB (Cognito path only)

Only required if using Cognito auth. Create two tables:

| Table | Partition key | Purpose |
|---|---|---|
| `yoloscribe-user-site` | `user_id` (S) | Maps user UUID → site name |
| `yoloscribe-api-tokens` | `token_id` (S) | Stores hashed API tokens |

`yoloscribe-api-tokens` also needs two GSIs: `user_id-index` (PK: `user_id`, SK: `created_at`) and `token_hash-index` (PK: `token_hash`).

Set `DYNAMODB_USER_SITE_TABLE` and `DYNAMODB_API_TOKENS_TABLE` if you use non-default names.

#### CloudFront + S3 — frontend hosting

Create an S3 bucket for the frontend build output and a CloudFront distribution pointing at it. Set `CLOUDFRONT_DOMAIN` and `FRONTEND_BUCKET`.

For video/audio support, configure a separate CloudFront cache behaviour for `*/assets/*` with signed cookies and `CachingDisabled` policy. Run `infra/scripts/setup_cloudfront_media.sh` to create the signing key pair and store the private key in Secrets Manager. Set `CLOUDFRONT_SIGNING_KEY_ID` and `CLOUDFRONT_MEDIA_DOMAIN`.

#### ACM — SSL certificates

Issue certificates for your backend domain and CloudFront distribution. CloudFront requires the certificate to be in `us-east-1` regardless of your deployment region.

---

### Deployment

Each service has a Dockerfile. Build and push images to GHCR or ECR, then deploy with the Helm charts in `infra/helm/`:

```bash
helm upgrade --install yoloscribe-backend infra/helm/yoloscribe-backend \
  -f backend.prod.values.yaml --namespace yoloscribe --create-namespace

helm upgrade --install yoloscribe-agent-runner infra/helm/yoloscribe-agent-runner \
  -f agent-runner.prod.values.yaml --namespace yoloscribe

helm upgrade --install yoloscribe-indexer infra/helm/yoloscribe-indexer \
  -f indexer.prod.values.yaml --namespace yoloscribe
```

Build and deploy the frontend:

```bash
cd frontend
VITE_SUPABASE_URL=... VITE_SUPABASE_ANON_KEY=... VITE_API_BASE=https://your-domain npm run build
aws s3 sync dist/ s3://$FRONTEND_BUCKET/ --delete
aws cloudfront create-invalidation --distribution-id $CLOUDFRONT_DISTRIBUTION_ID --paths "/*"
```
