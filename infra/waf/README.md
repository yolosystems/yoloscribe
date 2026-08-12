# WAF for the public LiteLLM ingress

Locks down the internet-facing LiteLLM ALB (`litellm-dev.yoloscribe.com`) to a
**default-deny allowlist**. Rationale, in one line: YoloScribe reaches LiteLLM
for *all* model/inference traffic over **internal K8s DNS**
(`http://litellm.yolo.svc.cluster.local:4000/v1`), so the only thing that
genuinely needs the public ingress is the **browser-driven MCP tool-OAuth
handshake** (`{LITELLM_MCP_URL}/mcp/{tool}`, `backend/routers/oauth.py`).

## ⚠️ Shared ALB — host-based scoping

The **same ALB** also serves the YoloScribe backend at `api-dev.yoloscribe.com`.
Because a WebACL is attached to the *load balancer* (not a hostname), it evaluates
**every** request to **both** hosts. The default-deny is meant only for the
LiteLLM host, so `allow-api-dev-host` matches the `Host` header
`api-dev.yoloscribe.com` and allows it — the backend has its own JWT auth and
path safety and must not be filtered here. Every allow/block rule *below* it
therefore applies only to `litellm-dev` (and any other host). If the backend ever
moves to a different hostname, update that rule.

> **Allow is terminating, so rule order is load-bearing.** Anything placed after
> `allow-api-dev-host` is dead for the backend host — evaluation stops the moment
> that rule matches. Rules that must apply to *every* host therefore have to sit
> **ahead** of it. `block-internal-paths` and `block-path-traversal` do; when
> adding a rule, decide deliberately which side of the host allow it belongs on.

## What the WebACL allows / blocks (litellm-dev host)

| Path | Public? | Why |
|---|---|---|
| `/mcp`, `/mcp/*` | ✅ allow | Browser delegated-PKCE OAuth handshake |
| `/.well-known/oauth-authorization-server*` | ✅ allow | OAuth AS discovery for the above |
| `/.well-known/oauth-protected-resource*` | ✅ allow | OAuth PRM discovery for the above |
| `/callback` | ✅ allow | Upstream IdP redirects back to the gateway's callback in delegated PKCE |
| `/{server}/authorize`, `/{server}/token`, `/{server}/register` | ✅ allow | Per-MCP-server OAuth endpoints (authorize / token / DCR) that LiteLLM's AS metadata advertises at the domain root, e.g. `/yoloscribe/authorize`. Matched by `^/[^/]+/(authorize\|token\|register)` — the suffix constraint means no inference/admin/management path qualifies, and deeper paths like `/v1/mcp/oauth/authorize` stay blocked. |
| everything else | ⛔ block (403) | Reached over internal DNS, never from the internet |

> **Why the discovery docs already worked but authorize didn't:** LiteLLM's protected-resource metadata (`/.well-known/oauth-protected-resource/mcp/{server}`) points at a per-server authorization server `https://{host}/{server}`, whose metadata (`/.well-known/oauth-authorization-server/{server}`) advertises `authorize`/`token`/`register` at the **domain root** (`/{server}/…`), not under `/mcp`. The `.well-known` prefixes were allowed; the root OAuth endpoints were not — hence the 403 at `/yoloscribe/authorize`.

Blocked-by-default includes: `/v1/chat/completions`, `/v1/completions`,
`/v1/embeddings`, `/v1/messages`, `/anthropic/*`, `/v1/models`, `/rerank`,
`/moderations` (inference); `/ui`, `/ui/*`, `/sso/*` (admin UI); `/key/*`,
`/user/*`, `/team/*`, `/organization/*`, `/spend/*`, `/global/*`, `/model/*`,
`/config/*`, `/audit/*` (management); `/docs`, `/redoc`, `/openapi.json`.

Rule order in the WebACL (evaluated top-down; first Allow/Block wins, else the
default Block):
- **`block-internal-paths`** (priority 0) — blocks `/internal/*` on **every**
  host. These are backend-to-backend endpoints (run-token minting, messaging
  channel resolution) reached over cluster DNS; in-cluster traffic goes
  pod → ClusterIP → pod and never passes through the ALB, so blocking them at
  the edge costs legitimate callers nothing. They carry a shared-secret header,
  but `/internal/runs/mint` takes an arbitrary `site` + `user_id`, so it must not
  be reachable from the internet on the strength of one secret alone.
- **`block-path-traversal`** (priority 1) — blocks any `..` in the (URL-decoded)
  path so an attacker can't smuggle `/mcp/../v1/chat/completions` past the allow
  rule, or `/foo/../internal/runs/mint` past the rule above.
- **`allow-api-dev-host`** (priority 2, Allow) — see the shared-ALB section above.
- **`rate-limit-mcp`** (priority 3) — 300 requests / 5 min per IP against the
  `/mcp` surface, to blunt abuse of the one exposed endpoint. Tune in the JSON.
  Note this sits *after* the host allow, so it does not apply to `api-dev`.
- **`allow-mcp-oauth`** (priority 4, Allow) — the allowlist in the table above.

> **ALB health checks are unaffected.** The ALB→target health check originates
> inside the VPC and is not evaluated by WAF, so no `/health/*` allow is needed.

## Deploy

```bash
cd infra/waf
REGION=us-west-2 ./install_litellm_waf.sh          # create or update the WebACL
# (AWS_PROFILE defaults to runyolo_admin; override if needed)
```

The script prints the **WebACL ARN**. WAF creation stops here — it's deliberately
kept separate from the ingress, which is **owned by the `yolobrain` repo** (that
repo deploys the shared LiteLLM proxy + its ALB ingress, namespace `yolo`).

**To enable the WAF**, add a single annotation to the LiteLLM ingress definition
**in yolobrain** and redeploy it there. The AWS Load Balancer Controller reconciles
the association — nothing to run in this repo, and don't associate manually too.

```yaml
# yolobrain: LiteLLM ingress `annotations:`  — add this one line
alb.ingress.kubernetes.io/wafv2-acl-arn: <PASTE WebACL ARN FROM THE SCRIPT>
```

To disable, remove the annotation and redeploy in yolobrain. The WebACL created
here is unaffected either way — it just sits unattached until an ingress references it.

**Optional defense-in-depth (also in yolobrain):** narrow the ingress `paths:`
so the ALB `404`s non-OAuth requests before WAF even evaluates them. Note this is
less complete than the WAF: the per-server OAuth roots (`/{server}/authorize|token
|register`) can only be narrowed by pinning each server name (e.g. `/yoloscribe`),
so new MCP servers would need new entries — the WAF's suffix rule covers them
generically. Rely on the WAF as the primary control; treat this as belt-and-braces:

```yaml
  hosts:
    - host: litellm-dev.yoloscribe.com
      paths:
        - { path: /mcp,                                    pathType: Prefix }
        - { path: /.well-known/oauth-authorization-server, pathType: Prefix }
        - { path: /.well-known/oauth-protected-resource,   pathType: Prefix }
        - { path: /callback,                               pathType: Prefix }
        - { path: /yoloscribe,                             pathType: Prefix }  # per MCP server
```

## Verify after attaching

```bash
B=https://litellm-dev.yoloscribe.com
# Inference must now be blocked at the edge (expect 403 from WAF, not 401):
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$B/v1/chat/completions" \
  -H 'Content-Type: application/json' -d '{"model":"haiku","messages":[]}'
# Admin/docs blocked:
for p in /ui/ /sso/key/generate /openapi.json /redoc; do
  curl -s -o /dev/null -w "%{http_code}  $p\n" "$B$p"; done
# OAuth discovery + gateway callback still reachable (expect 200/302/401/404 from
# LiteLLM, NOT a WAF 403):
curl -s -o /dev/null -w "%{http_code}\n" "$B/.well-known/oauth-protected-resource"
curl -s -o /dev/null -w "%{http_code}\n" "$B/callback"
# Traversal smuggling blocked (expect 403):
curl -s -o /dev/null -w "%{http_code}\n" "$B/mcp/../v1/models"
```

Internal traffic is unaffected — the backend and agent-runner keep using
`litellm.yolo.svc.cluster.local:4000` and never traverse the ALB/WAF.

## Caveat — confirm the exact OAuth paths

LiteLLM's MCP-OAuth path scheme can vary by chart/version. Run **one real tool
enrollment** and watch which `Host`/paths the browser is redirected to during
the authorize step:
- If the discovered `authorization_endpoint` is on the **upstream** (Linear,
  GitHub, …) rather than LiteLLM, the allowlist can be narrowed further — LiteLLM
  then only needs to serve `/.well-known/*` discovery, not a browser authorize.
- If LiteLLM exposes the authorize/token/register at a path **outside** `/mcp`
  and `/.well-known`, add that prefix to `allow-mcp-oauth` in
  `litellm-ingress-web-acl.json` and re-run the install script.

## Most-secure alternative — no public ingress at all

If browser tool-OAuth isn't in use (no `mcp_servers` configured in LiteLLM), set
`litellmMcpUrl: ""` in the backend values (the values comment: *"Leave empty to
disable tool OAuth"*) and drop the LiteLLM ingress entirely. LiteLLM then has
**zero** public exposure and is reachable only via cluster DNS — no WAF required.
