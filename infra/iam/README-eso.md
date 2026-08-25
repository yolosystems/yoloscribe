# External Secrets Operator — IAM scope

`yoloscribe-eso-policy.json` is the policy the ESO ServiceAccount's IRSA role
needs to sync YoloScribe's deployment secrets. It grants read on exactly one
prefix:

```
arn:aws:secretsmanager:<region>:<account>:secret:yoloscribe/deploy/*
```

## Why not `yoloscribe/*`

Because that prefix is where the *application* keeps per-user secrets at
runtime:

```
yoloscribe/{user-uuid}/oauth/github        access + refresh tokens
yoloscribe/{user-uuid}/oauth/linear
yoloscribe/{user-uuid}/oauth/google-workspace
yoloscribe/{user-uuid}/litellm-key         that user's LiteLLM virtual key
yoloscribe/{user-uuid}/webhooks
yoloscribe/platform/oauth/*                platform OAuth client credentials
yoloscribe/cloudfront-signing-key
yoloscribe/run-token-signing-key
```

Granting `yoloscribe/*` would let a component whose only job is to publish
deployment configuration into Kubernetes Secrets read every user's third-party
OAuth tokens. ESO also *materialises* what it reads as a Kubernetes Secret, so
the blast radius is not theoretical: a misconfigured `dataFrom` would copy user
tokens into a namespace-readable object.

Deployment secrets therefore live under their own prefix, `yoloscribe/deploy/`,
which nothing at runtime reads or writes.

## Objects the operator expects

Each is a JSON object; every key is copied verbatim into the target Kubernetes
Secret, so the key names must match what the consuming chart reads.

**A key the deployment reads but the object omits is a hard failure**, not a
default: the pod stops at `CreateContainerConfigError`. Which keys the backend
reads depends on how it is configured, so the parenthetical conditions below are
load-bearing — an OIDC deployment must *not* carry `supabase-service-role-key`,
and a deployment with no messaging bot must not carry `messaging-bot-secret`.

| Secrets Manager key | Kubernetes Secret | Keys |
|---|---|---|
| `yoloscribe/deploy/backend` | `yoloscribe-backend` | `anthropic-api-key` (always), `supabase-service-role-key` (when `authProvider: supabase`), `litellm-api-key` (when `config.litellmBaseUrl` set), `messaging-bot-secret` (when `messagingBotEnabled`), `yolobrain-internal-secret` (when `yolobrain.apiUrl` set) |
| `yoloscribe/deploy/agent-runner` | `yoloscribe-agent-runner` | `anthropic-api-key`, `litellm-api-key` |
| `yoloscribe/deploy/otel` | `yoloscribe-backend-otel`, `yoloscribe-agent-runner-otel` | `otlp-headers` |
| `yoloscribe/deploy/ghcr` | `yoloscribe-backend-ghcr`, `yoloscribe-agent-runner-ghcr` | `username`, `pat` |

The `ghcr` object is the exception to "copied verbatim": a registry credential
has to be a `kubernetes.io/dockerconfigjson` Secret, so the chart templates one
from `username` and `pat` rather than copying keys across.

## Attaching it

The operator runs one ServiceAccount for the whole cluster, shared with
YoloBrain, whose role already carries `yolobrain-secrets-ro`
(`secretsmanager:GetSecretValue` on `yolobrain/*`). Add this policy alongside it
rather than replacing it — both products' prefixes must be readable, and neither
grant should widen to cover the other's.

```bash
aws iam create-policy --policy-name yoloscribe-eso-ro \
  --policy-document file://infra/iam/yoloscribe-eso-policy.json
aws iam attach-role-policy --role-name <eso-role> \
  --policy-arn arn:aws:iam::<account>:policy/yoloscribe-eso-ro
```

## Rotation

ESO re-reads on its refresh interval (1h by default) and updates the Kubernetes
Secret in place. **Pods are not restarted**, so a rotated value does not reach a
running process until the deployment rolls. Rotate, then `kubectl rollout
restart`, or accept that the change lands with the next release.

Editing the Kubernetes Secret directly does nothing lasting: `creationPolicy:
Owner` means ESO reverts it on the next sync. Change the value in Secrets
Manager.
