# WAF for the public LiteLLM ingress

Locks down the internet-facing LiteLLM ALB (`litellm-dev.yoloscribe.com`) to a
**default-deny allowlist**. Rationale, in one line: YoloScribe reaches LiteLLM
for *all* model/inference traffic over **internal K8s DNS**
(`http://litellm.yolo.svc.cluster.local:4000/v1`), so the only thing that
genuinely needs the public ingress is the **browser-driven MCP tool-OAuth
handshake** (`{LITELLM_MCP_URL}/mcp/{tool}`, `backend/routers/oauth.py`).

## What the WebACL allows / blocks

| Path | Public? | Why |
|---|---|---|
| `/mcp`, `/mcp/*` | ✅ allow | Browser delegated-PKCE OAuth handshake |
| `/.well-known/oauth-authorization-server*` | ✅ allow | OAuth AS discovery for the above |
| `/.well-known/oauth-protected-resource*` | ✅ allow | OAuth PRM discovery for the above |
| everything else | ⛔ block (403) | Reached over internal DNS, never from the internet |

Blocked-by-default includes: `/v1/chat/completions`, `/v1/completions`,
`/v1/embeddings`, `/v1/messages`, `/anthropic/*`, `/v1/models`, `/rerank`,
`/moderations` (inference); `/ui`, `/ui/*`, `/sso/*` (admin UI); `/key/*`,
`/user/*`, `/team/*`, `/organization/*`, `/spend/*`, `/global/*`, `/model/*`,
`/config/*`, `/audit/*` (management); `/docs`, `/redoc`, `/openapi.json`.

Extra guardrails in the WebACL:
- **`block-path-traversal`** (priority 0) — blocks any `..` in the (URL-decoded)
  path so an attacker can't smuggle `/mcp/../v1/chat/completions` past the allow
  rule.
- **`rate-limit-mcp`** (priority 1) — 300 requests / 5 min per IP against the
  `/mcp` surface, to blunt abuse of the one exposed endpoint. Tune in the JSON.

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
to the three OAuth prefixes so the ALB `404`s non-OAuth requests before WAF even
evaluates them:

```yaml
  hosts:
    - host: litellm-dev.yoloscribe.com
      paths:
        - { path: /mcp,                                    pathType: Prefix }
        - { path: /.well-known/oauth-authorization-server, pathType: Prefix }
        - { path: /.well-known/oauth-protected-resource,   pathType: Prefix }
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
# OAuth discovery still reachable (expect 200/401/404 from LiteLLM, NOT a WAF 403):
curl -s -o /dev/null -w "%{http_code}\n" "$B/.well-known/oauth-protected-resource"
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
