# Public exposure policy

What YoloScribe requires of whatever sits in front of it, and why. This is a
statement of requirements, not a deployment guide — it holds whether you run the
WAF we run, a different WAF, an nginx ingress with path rules, or nothing at all.

**The enforcement lives elsewhere.** In the runyolo account it is an AWS WAFv2
WebACL on the shared ALB, provisioned from the private `yoloscribe-ops` repo
(`waf/`). Self-hosting is a different deployment with the same requirements —
meet them however your infrastructure does that.

---

## Must never be publicly reachable

**`/internal/*`** — backend-to-backend endpoints: run-token minting
(`/internal/runs/mint`) and messaging channel resolution
(`/internal/messaging/*`). See `CLAUDE.md` → *Internal endpoints*.

These carry a shared-secret header, so why block them at the edge as well?
Because `/internal/runs/mint` accepts an **arbitrary `site` + `user_id`** —
anything holding that secret can act as any user. One leaked secret should not
be remotely exploitable, and blocking the path at the edge costs legitimate
callers nothing: every real caller is in-cluster, and pod → ClusterIP → pod never
traverses the load balancer.

The two controls are a pair, and neither is sufficient alone. The secret check
is what actually authorizes, because any pod in the cluster can reach a
ClusterIP; the edge block is what keeps a leaked secret from being usable from
the internet.

**The consequence for callers:** anything in-cluster that talks to these routes
must be configured with the **internal service address**, never the public
hostname. A public URL returns 403 on every request. This is the first thing to
check when a bot or worker cannot reach the backend.

**Path traversal.** `..` in a decoded path must be rejected, or an allow rule for
one prefix becomes an allow rule for everything behind it —
`/mcp/../v1/chat/completions`, or `/foo/../internal/runs/mint` past the rule
above.

## Must stay publicly reachable

Only the browser-driven MCP tool-OAuth handshake genuinely needs public ingress
on the LiteLLM host. Everything else — all inference, admin and management — is
reached over internal cluster DNS (`http://litellm.yolo.svc.cluster.local:4000/v1`).

| Path | Why |
|---|---|
| `/mcp`, `/mcp/*` | The delegated-PKCE handshake itself |
| `/.well-known/oauth-authorization-server*` | AS discovery for it |
| `/.well-known/oauth-protected-resource*` | Protected-resource discovery for it |
| `/callback` | Where the upstream IdP redirects back |
| `/{server}/authorize`, `/{server}/token`, `/{server}/register` | Per-MCP-server OAuth endpoints, which LiteLLM's AS metadata advertises at the **domain root** (e.g. `/yoloscribe/authorize`) rather than under `/mcp` |

That last row is the non-obvious one, and it is why discovery can appear to work
while enrollment still fails with a 403 on `/{server}/authorize`.

Everything else on that host should be blocked: `/v1/*` and `/anthropic/*`
(inference), `/ui`, `/sso/*` (admin), `/key/*`, `/user/*`, `/team/*`,
`/organization/*`, `/spend/*`, `/global/*`, `/model/*`, `/config/*`, `/audit/*`
(management), `/docs`, `/redoc`, `/openapi.json`.

## Avoiding the requirement entirely

If browser tool-OAuth is not in use — no `mcp_servers` configured in LiteLLM —
set `litellmMcpUrl: ""` in the backend values and drop the LiteLLM ingress
altogether. LiteLLM then has **zero** public exposure, reachable only over cluster
DNS, and none of the above applies. This is the most secure configuration and the
right default for a deployment that does not enroll third-party tools.

## A note on shared load balancers

If YoloScribe shares a load balancer with other services, a filter attached there
evaluates **every** request to **every** host on it, because the association is
to the load balancer and not to a hostname. A default-deny meant for one host
will silently 403 the others until each has an allow rule. Worth knowing before
adding a hostname to an existing group.
