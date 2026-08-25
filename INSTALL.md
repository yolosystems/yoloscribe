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

#### Any OIDC provider (alternative — Auth0, Keycloak, Okta, Entra, Cognito)

YoloScribe can authenticate against any OIDC-compliant identity provider. Set `AUTH_PROVIDER=oidc` on the backend and `VITE_AUTH_PROVIDER=oidc` on the frontend build. Both sides are discovery-driven: point them at the provider's `.well-known/openid-configuration` and the endpoints and signing keys are read from it.

- Register a **public / SPA client** using authorization code + PKCE. Do not issue a client secret — the browser cannot hold one, and the backend is a pure resource server that only *validates* tokens. Neither ever needs it.
- Set the client's redirect URI to your site origin (e.g. `https://app.yoloscribe.com`); the browser client uses `window.location.origin`.
- Enable the `offline_access` scope if you want sessions to renew silently rather than ending at the access-token lifetime.

Backend: `OIDC_CONFIG_URL` (required), plus optional `OIDC_CLIENT_ID`, `OIDC_AUDIENCE`, `OIDC_ISSUER`. Frontend build: `VITE_OIDC_CONFIG_URL`, `VITE_OIDC_CLIENT_ID`, and the optional `VITE_OIDC_SCOPE` / `VITE_OIDC_TOKEN` — see `frontend/.env.example`. Create the DynamoDB tables (see below). Per-user infrastructure is provisioned on first sign-in via the authenticated `POST /provision` onboarding flow, same as the Supabase path.

**Audience must agree across the two sides.** By default the browser sends the **ID token** as the bearer, whose `aud` is the client ID, and the backend defaults its expected audience to `OIDC_CLIENT_ID`. If you instead configure an API audience via `OIDC_AUDIENCE`, set `VITE_OIDC_TOKEN=access` so the browser sends the access token carrying that audience. A mismatch shows up as a 401 on every authenticated request.

**Cognito is configured this way too** — it publishes a standard discovery document at `https://cognito-idp.{region}.amazonaws.com/{userPoolId}/.well-known/openid-configuration`. The backend additionally accepts `AUTH_PROVIDER=cognito`, which behaves identically for token validation but adds an admin `delete_user` call that generic OIDC has no standard equivalent for. The frontend has no Cognito-specific mode; use `oidc`.

> **Invite links are Supabase-only.** The magic-link invite flow (`inviteUserByEmail` and the expired-link page) is a Supabase feature. Under `oidc` the identity provider owns sign-up, and YoloScribe provisions on first successful sign-in instead.

**Keep `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` set even under `oidc`, if inbound MCP clients authorize through Supabase.** The `/oauth/consent` screen is the approval UI for Supabase's headless OAuth 2.1 server — the flow LiteLLM and Claude Code use to authorize against your MCP endpoint. That is independent of how your own users sign in, so the frontend builds it whenever a Supabase project is configured rather than only when Supabase is the login provider. Drop those two variables and `/oauth/consent` reports that the deployment isn't configured for Supabase OAuth, breaking third-party MCP authorization.

**Using Supabase itself as the OIDC provider** is the cheapest way to prove the generic path before introducing a third-party IdP — same users, same tokens, and its discovery document advertises everything the browser client needs (`S256` PKCE, `none` token-endpoint auth, `authorization_code` + `refresh_token`, `offline_access`, `query` response mode, ES256 keys). Create an OAuth app in the Supabase dashboard as a public/PKCE client with the site origin as its redirect URI — there is no `registration_endpoint`, so it must be registered by hand — then point `OIDC_CONFIG_URL` / `VITE_OIDC_CONFIG_URL` at `<project>/auth/v1/.well-known/openid-configuration` and use the app's client ID on both sides. Note Supabase publishes no `end_session_endpoint`, so sign-out clears the local session without ending the Supabase one.

#### Messaging bot (optional)

- Create a Discord application at [discord.com/developers](https://discord.com/developers/applications)
- Create a bot user, enable the **Message Content** privileged intent, and copy the bot token (`DISCORD_BOT_TOKEN`)
- Generate a shared secret: `openssl rand -hex 32` → set as `MESSAGING_BOT_SECRET` on **both** the bot and the backend (the backend also needs `messagingBotEnabled=true`)
- Set `YOLOSCRIBE_API_URL` to the backend's **in-cluster** service DNS, e.g. `http://yoloscribe-backend.yolo.svc.cluster.local:8000`
- Set `ENABLED_ADAPTERS` to the comma-separated list of platform adapters to enable (currently `discord`)

The bot holds **no** database credential, no encryption key, and no user API token. It authenticates to the backend's `/internal/messaging/*` endpoints with `MESSAGING_BOT_SECRET` and names a channel; the backend resolves channel → API token → owning site and runs the request as that user. A user's token is handled exactly once, during `/setup`, and is forwarded rather than stored — the stored binding records only the token's ID, so revoking a token disconnects its channels automatically.

> **`MESSAGING_BOT_SECRET` must differ from `INTERNAL_MINT_SECRET`.** The bot processes untrusted input from chat platforms, and `/internal/runs/mint` accepts an arbitrary `site` + `user_id`. One shared value would let a compromised bot mint run tokens for any site. `install_messaging_bot.sh` refuses to deploy if the two match.

> **`YOLOSCRIBE_API_URL` must be the in-cluster address, not the public hostname.** `/internal/*` is blocked at the ALB by the WAF (see `infra/waf/README.md`), so a public URL here returns 403 on every request. In-cluster traffic goes pod → ClusterIP → pod and never reaches the load balancer.

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
| [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/) | Provisions the ALB for each `Ingress` (backend, and the LiteLLM proxy). Required for the `className: alb` ingresses, and for the `wafv2-acl-arn` annotation if you front them with a WAF (see `infra/waf/README.md` for what must be blocked). |
| [ExternalDNS](https://kubernetes-sigs.github.io/external-dns/) | Creates the Route 53 records for ingress hostnames. EKS gives you no DNS automation out of the box. |
| [EBS CSI driver](https://github.com/kubernetes-sigs/aws-ebs-csi-driver) | Dynamic `PersistentVolume` provisioning for any stateful dependency you self-host in-cluster (e.g. Postgres, Phoenix). Auto Mode ships its own (`ebs.csi.eks.amazonaws.com`) and does not need this addon. |
| A gp3 `StorageClass` | Nothing YoloScribe runs is stateful, but its in-cluster dependencies are. **Check whether one is marked default** — a PVC that omits `storageClassName` on a cluster with no default stays `Pending` indefinitely, and nothing is reported on the pod. |
| An external **Postgres** (RDS or equivalent) | Required by LiteLLM and Phoenix. Both vendor charts default to deploying their *own* Postgres; both must be configured against an external one instead — see the storage note in `phoenix.example.values.yaml`. Neither product's own data lives there; YoloScribe's own state is S3 and DynamoDB. |
| [External Secrets Operator (ESO)](https://external-secrets.io/) | Syncs AWS Secrets Manager → Kubernetes `Secret`s. Point a chart's `existingSecret` value at an ESO-materialized secret to keep plaintext out of Helm `--set` / release values (recommended over injecting secrets at install time). |

> **On EKS Auto Mode, do not install the AWS Load Balancer Controller.** Auto Mode provides its own (`eks.amazonaws.com/alb`) and is driven by `IngressClass` + `IngressClassParams` instead of the legacy `alb.ingress.kubernetes.io/*` annotations, which it ignores. Set `ingress.controllerType: eks-auto-mode` and configure the class under `ingressClass:`. Auto Mode still has no Route 53 integration, so ExternalDNS is installed separately either way.

> **Sharing one ALB across services** is done with `ingressClass.group` — every service naming the same group lands on the same load balancer, whether or not they share an `IngressClass`. If you do point several releases at one class, exactly one may set `ingressClass.create: true`: Helm refuses to adopt a resource another release created, so a second creator fails on install and on every upgrade after. Give that release `keepOnUninstall: true` so removing it does not delete the class out from under the others.

#### Secrets Manager

No manual setup required. The backend creates per-user secret prefixes (`yoloscribe/{user_id}/`) automatically when users connect skills (GitHub, Linear, etc.). The backend IAM role needs `secretsmanager:CreateSecret`, `PutSecretValue`, `GetSecretValue`, `DescribeSecret` on `yoloscribe*` resources.

#### DynamoDB (non-Supabase auth only)

Required when `AUTH_PROVIDER` is `oidc` or `cognito` — these paths keep user data in DynamoDB rather than Supabase tables. Not needed on the Supabase path. Create three tables:

| Table | Partition key | Purpose |
|---|---|---|
| `yoloscribe-user-site` | `user_id` (S) | Maps user UUID → site name |
| `yoloscribe-api-tokens` | `token_id` (S) | Stores hashed API tokens |
| `yoloscribe-messaging-configs` | `id` (S) | Messaging bot channel bindings |

GSIs: `yoloscribe-api-tokens` needs `user_id-index` (PK: `user_id`, SK: `created_at`) and `token_hash-index` (PK: `token_hash`); `yoloscribe-messaging-configs` needs `api_token_id-index` (PK: `api_token_id`).

`infra/scripts/setup_dynamodb.sh` creates all three with the right keys and indexes. Set `DYNAMODB_USER_SITE_TABLE`, `DYNAMODB_API_TOKENS_TABLE`, and `DYNAMODB_MESSAGING_CONFIGS_TABLE` if you use non-default names.

The backend's IAM role needs DynamoDB access to these tables — see the `DynamoDBUserStores` statement in `infra/iam/yoloscribe-backend-policy.json`.

Also note `yoloscribe-messaging-configs` needs a second GSI, `platform_channel-index` (PK: `platform_channel`), used to resolve an inbound chat message to its owning site. `platform_channel` is a derived `"{platform}:{channel_id}"` attribute, because DynamoDB cannot index into the nested `connection` map. The setup script adds it to existing tables in place as well as creating it on new ones.

#### Migrating an existing install from Supabase to DynamoDB

Only `user_site` is migrated. This is a deliberate decision — the three tables move, or don't, as a set:

| Table | Migrated | Why |
|---|---|---|
| `user_site` | **yes** | Without it a user cannot resolve their site, and there's no way for them to recreate it |
| `api_tokens` | no | Users generate a new token after cutover |
| `messaging_configs` | no | A binding's only link to an owner is `api_token_id`; regenerated tokens get new UUIDs, so migrated rows would reference IDs that don't exist and every channel would resolve to "not linked" |

```bash
AWS_PROFILE=... AWS_REGION=... SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
  uv run python backend/migrate_supabase_to_dynamodb.py --dry-run   # then without
```

After cutover, each user generates a new API token in the UI and re-runs `/setup` in every connected chat channel. They must do the second step regardless, since the token they pasted at setup time no longer exists.

> If you'd rather cut over silently, migrate `api_tokens` too, preserving `id` and `token_hash` — then migrating `messaging_configs` becomes meaningful. It must be both or neither: migrating `messaging_configs` alone produces rows that look correct and are all dead.

#### CloudFront + S3 — frontend hosting

Create an S3 bucket for the frontend build output and a CloudFront distribution pointing at it. Set `CLOUDFRONT_DOMAIN` and `FRONTEND_BUCKET`.

For video/audio support, configure a separate CloudFront cache behaviour for `*/assets/*` with signed cookies and `CachingDisabled` policy. Run `infra/scripts/setup_cloudfront_media.sh` to create the signing key pair and store the private key in Secrets Manager. Set `CLOUDFRONT_SIGNING_KEY_ID` and `CLOUDFRONT_MEDIA_DOMAIN`.

#### ACM — SSL certificates

Issue certificates for your backend domain and CloudFront distribution. CloudFront requires the certificate to be in `us-east-1` regardless of your deployment region.

---

### Deployment

Each service has a Dockerfile. Build and push images to GHCR or ECR, then deploy with the `install_*.sh` scripts in `infra/helm/`. Every script takes the same inputs and runs `helm upgrade --install`, so the same command creates a release and updates it:

```bash
export STAGE=prod REGION=us-west-2 K8S_NAMESPACE=yoloscribe

infra/helm/install_backend.sh
infra/helm/install_runner.sh
infra/helm/install_indexer.sh
infra/helm/install_messaging_bot.sh   # optional; see the messaging bot section
```

| Input | Required | Purpose |
|---|---|---|
| `STAGE` | yes | Names the values file — `dev`, `staging`, `prod` |
| `REGION` | yes | Names the values file — e.g. `us-west-2` |
| `K8S_NAMESPACE` | yes | Target namespace. **No default**, deliberately: combined with `--create-namespace`, a default would turn a forgotten variable into a second copy of the stack in a namespace nobody meant to create. `NAMESPACE` is accepted as an alias. |
| `--values-dir <path>` | no | Where to look for the values file; defaults to `infra/helm/` |
| `--dry-run` | no | Render templates without touching the cluster |

Anything else you pass is forwarded to `helm` unchanged (`--timeout 10m`, `--atomic`, and so on).

Each script resolves `<component>.<STAGE>.<REGION>.values.yaml` — `backend`, `agent-runner`, `indexer`, `messaging-bot`, `litellm`. Copy the matching `*.example.values.yaml` and fill it in. These files carry account-specific detail and are gitignored; if you keep them in a separate ops repo, point at it rather than copying:

```bash
infra/helm/install_backend.sh --values-dir ~/ops/helm
```

Resolution is strict — with `--values-dir` there is no fallback to `infra/helm/`, so it is always unambiguous which file a deploy used. Both `--values-dir <path>` and `--values-dir=<path>` work, including a leading `~`.

Values come from the file; secrets come from the environment or the repo-root `.env`. For the four variables that decide *where* a deploy lands — `STAGE`, `REGION`, `K8S_NAMESPACE`, `NAMESPACE` — an explicit environment variable overrides `.env`, so `STAGE=prod infra/helm/install_backend.sh` means prod even when `.env` says otherwise.

Build and deploy the frontend:

```bash
cd frontend
VITE_SUPABASE_URL=... VITE_SUPABASE_ANON_KEY=... VITE_API_BASE=https://your-domain npm run build
aws s3 sync dist/ s3://$FRONTEND_BUCKET/ --delete
aws cloudfront create-invalidation --distribution-id $CLOUDFRONT_DISTRIBUTION_ID --paths "/*"
```
