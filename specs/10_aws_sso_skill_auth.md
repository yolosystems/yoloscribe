## AWS SSO Skill Authentication

This document describes how YoloScribe should support skills that use AWS MCP servers, where the required credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`) are obtained via the AWS IAM Identity Center (SSO) OIDC device authorization flow rather than copy-pasted by the user.

> **DO NOT IMPLEMENT NOW.** This is a planning document only.

---

### Background

#### The existing OAuth flow

The existing skills (GitHub, Linear, Google Workspace) authenticate via the standard OAuth 2.0 authorization code + PKCE flow:

1. Backend calls `POST /oauth/initiate/{skill_name}` → performs discovery or uses a pre-registered client → returns an `auth_url`.
2. Frontend redirects the browser to `auth_url`.
3. The OAuth server redirects back to `GET /oauth/callback` on the backend with an authorization code.
4. Backend exchanges the code for tokens and stores them in Secrets Manager.

This works because those OAuth servers support standard redirect-based flows.

#### Why AWS SSO is different

AWS IAM Identity Center uses the **OAuth 2.0 Device Authorization Grant** (RFC 8628) rather than the redirect flow. The reasons are:

- AWS SSO can't redirect back to an arbitrary `redirect_uri` the way a standard OAuth server can (at least not for CLI/backend use cases).
- The flow is designed for situations where the "device" doing the work (the backend) is separate from the "browser" doing the authentication.
- It's the same flow used by the AWS CLI when you run `aws sso login`.

#### Why there is no pre-existing `client_id` to find

For skills like GitHub, a `client_id` exists because someone registered an OAuth App in GitHub's developer portal. For AWS SSO there is no portal to register with. Instead, the AWS SSO OIDC service exposes a `RegisterClient` API call (via `boto3.client('sso-oidc')`) that issues a `clientId` and `clientSecret` on demand — essentially built-in Dynamic Client Registration. The registration:

- Is per-AWS-region and per-client-name.
- Requires no upfront configuration in any console.
- Returns credentials valid for 90 days, after which `RegisterClient` is called again.
- Is scoped to a particular SSO instance by the `startUrl` passed at device authorization time.

This means **no operator action is needed to find or create a client_id** — the backend creates one itself the first time a user initiates an AWS SSO auth.

---

### New skill type: `aws-sso`

A new `auth` block is added to `mcp.json` to identify AWS SSO skills:

```json
{
  "mcpServers": {
    "aws-s3": {
      "command": "uvx",
      "args": ["awslabs.s3-mcp-server"],
      "env": {
        "AWS_ACCESS_KEY_ID": "${AWS_SSO_ACCESS_KEY_ID}",
        "AWS_SECRET_ACCESS_KEY": "${AWS_SSO_SECRET_ACCESS_KEY}",
        "AWS_SESSION_TOKEN": "${AWS_SSO_SESSION_TOKEN}",
        "AWS_REGION": "us-east-1"
      }
    }
  },
  "auth": {
    "type": "aws-sso",
    "region": "us-east-1",
    "ssoStartUrl": "https://myorg.awsapps.com/start"
  }
}
```

The `${AWS_SSO_*}` placeholders are substituted from Secrets Manager at agent-run time using the existing `${VAR}` substitution mechanism already present in the worker. The `auth` block is used by the backend to recognize the skill as AWS SSO type and drive the credential-acquisition flow.

The `ssoStartUrl` is organization-specific and is set by the operator when deploying the skill to S3. Users do not need to know or enter it.

---

### The Device Authorization Flow

```
Backend                         AWS SSO OIDC                  User's Browser
   |                                  |                              |
   |-- RegisterClient (once/region) ->|                              |
   |<- clientId, clientSecret --------|                              |
   |                                  |                              |
   |-- StartDeviceAuthorization ------>|                              |
   |<- deviceCode, userCode,          |                              |
   |   verificationUriComplete --------|                              |
   |                                  |                              |
   |--- return verificationUri, ------>  Frontend opens in browser ->|
   |    userCode, job_id to frontend  |                              |
   |                                  |         (user logs in) ----->|
   |                                  |<-- approval granted ---------|
   |                                  |                              |
   |-- (polling) CreateToken -------->|                              |
   |<- accessToken, refreshToken -----|                              |
   |                                  |                              |
   |-- ListAccounts ----------------->|  (via sso client)            |
   |-- ListAccountRoles ------------->|                              |
   |                                  |                              |
   |  (if >1 account/role: return     |                              |
   |   choices to frontend for user   |                              |
   |   selection)                     |                              |
   |                                  |                              |
   |-- GetRoleCredentials ----------->|                              |
   |<- accessKeyId, secretAccessKey,  |                              |
   |   sessionToken, expiration -------|                              |
   |                                  |                              |
   |-- store all in Secrets Manager   |                              |
```

---

### Client Registration Caching

`RegisterClient` is cheap but its output has a 90-day TTL. The backend caches the registration in Secrets Manager at:

```
yoloscribe/platform/aws-sso/{region}/client
```

Format:
```json
{
  "client_id": "...",
  "client_secret": "...",
  "expires_at": 1234567890
}
```

On each `POST /oauth/aws-sso/initiate/{skill_name}`, the backend reads this secret. If absent or expired, it calls `sso-oidc:RegisterClient` and writes a fresh entry. The IAM policy for the backend service account needs `secretsmanager:GetSecretValue` and `secretsmanager:PutSecretValue` on `yoloscribe/platform/aws-sso/*` (similar to the existing per-user secret access it already has).

---

### New API Endpoints

#### `POST /oauth/aws-sso/initiate/{skill_name}`

Starts the device authorization flow.

1. Load `mcp.json` for the skill from S3 and confirm `auth.type == "aws-sso"`. Extract `auth.region` and `auth.ssoStartUrl`.
2. Load or create the cached client registration for the region.
3. Call `sso-oidc:StartDeviceAuthorization` with `clientId`, `clientSecret`, `startUrl`.
4. Store pending state keyed by a backend-generated `job_id` (similar to the existing `_oauth_pending` dict):
   - `device_code`, `interval`, `expires_at`, `user_id`, `skill_name`, `region`, `sso_start_url`, `client_id`, `client_secret`
5. Return to frontend:

```json
{
  "job_id": "abc123",
  "verification_uri": "https://device.sso.us-east-1.amazonaws.com/",
  "verification_uri_complete": "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD-EFGH",
  "user_code": "ABCD-EFGH",
  "expires_in": 600
}
```

The frontend opens `verification_uri_complete` in a new tab (or the platform browser). The user sees a pre-filled code and just clicks Approve.

#### `GET /oauth/aws-sso/status/{skill_name}?job_id={job_id}`

The frontend polls this after opening the verification URL. The backend calls `sso-oidc:CreateToken` with the `device_code` stored for `job_id`.

Possible responses:

| AWS exception | Response |
|---|---|
| `AuthorizationPendingException` | `{"status": "pending"}` |
| `SlowDownException` | `{"status": "pending"}` (frontend should increase poll interval) |
| `ExpiredTokenException` | `{"status": "expired"}` |
| Success | see below |

On success:
1. Call `sso:ListAccounts` and `sso:ListAccountRoles` using the returned SSO `accessToken`.
2. If exactly one account and one role: call `sso:GetRoleCredentials`, store everything, return:
   ```json
   { "status": "success" }
   ```
3. If multiple accounts or roles: return:
   ```json
   {
     "status": "needs_selection",
     "accounts": [
       {
         "account_id": "123456789012",
         "account_name": "my-org-prod",
         "roles": ["AWSAdministratorAccess", "ReadOnlyAccess"]
       }
     ]
   }
   ```
   The pending state is updated to store the SSO `accessToken` (needed for `GetRoleCredentials` in the next call).

The frontend polls on the `interval` value returned by `StartDeviceAuthorization` (typically 5 seconds).

#### `POST /oauth/aws-sso/select/{skill_name}`

Called only when status returned `needs_selection`. Body:

```json
{
  "job_id": "abc123",
  "account_id": "123456789012",
  "role_name": "AWSAdministratorAccess"
}
```

Backend calls `sso:GetRoleCredentials`, stores everything, returns `{ "status": "success" }`.

---

### Credential Storage

All data for a skill is stored in a single Secrets Manager secret at:

```
yoloscribe/{user_id}/aws-sso/{skill_name}
```

```json
{
  "sso_access_token": "...",
  "sso_refresh_token": "...",
  "sso_token_expires_at": 1234567890,
  "account_id": "123456789012",
  "role_name": "AWSAdministratorAccess",
  "region": "us-east-1",
  "sso_start_url": "https://myorg.awsapps.com/start",
  "access_key_id": "ASIA...",
  "secret_access_key": "...",
  "session_token": "...",
  "credential_expires_at": 1234567890
}
```

Additionally, the three MCP-injectable values are written as individual secrets using the existing pattern so the existing `${VAR}` substitution in the agent runner works without modification:

```
yoloscribe/{user_id}/AWS_SSO_ACCESS_KEY_ID
yoloscribe/{user_id}/AWS_SSO_SECRET_ACCESS_KEY
yoloscribe/{user_id}/AWS_SSO_SESSION_TOKEN
```

These individual secrets are written whenever `GetRoleCredentials` is called (initial auth and every refresh).

---

### `GET /secrets/status` changes

`_is_remote_skill` currently detects OAuth skills by checking for a `url` field in `mcp.json`. A new helper `_is_aws_sso_skill` reads `auth.type == "aws-sso"` from `mcp.json`. The `get_secrets_status` endpoint adds a third branch:

```python
if _is_aws_sso_skill(skill_name):
    token = _load_aws_sso_token(user_id, skill_name)
    skills[skill_name] = {
        "type": "aws-sso",
        "authenticated": token is not None,
        "account_id": token.get("account_id") if token else None,
        "role_name": token.get("role_name") if token else None,
        "credential_expires_at": ...,
        "sso_token_expires_at": ...,
    }
```

---

### Credential Refresh

AWS SSO credentials have two expiry timers:

| Token | Typical TTL | Renewable? |
|---|---|---|
| Role credentials (`accessKeyId` etc.) | 1–12 hours (configured by SSO admin) | Yes, via SSO access token |
| SSO access token | 8 hours | Yes, via SSO refresh token |
| SSO refresh token | 90 days | No — requires re-auth |

**Strategy: refresh on use, not on a schedule.**

Before each agent run, the agent runner checks `credential_expires_at` for any AWS SSO skill in the job. If within 5 minutes of expiry:

1. Load the `aws-sso` blob from Secrets Manager.
2. If SSO access token is still valid: call `sso:GetRoleCredentials` → write fresh individual secrets.
3. If SSO access token is expired but refresh token is valid: call `sso-oidc:CreateToken` with `grant_type=refresh_token` → get new SSO access token → then `GetRoleCredentials` → write all.
4. If refresh token is expired (>90 days since last auth): mark the skill as unauthenticated (set `sso_access_token: null` in the blob). The user will see the skill as requiring re-authentication in the Credentials panel.

The web process (`main.py`) also checks expiry in `GET /secrets/status` and surfaces `credential_expires_at` and `sso_token_expires_at` so the frontend can show warnings before credentials go stale.

---

### Frontend Changes

The Credentials panel adds a third skill card type (`type: "aws-sso"`) alongside the existing `oauth` and `key` types.

**Unauthenticated state:**
- "Connect AWS account" button → calls `POST /oauth/aws-sso/initiate/{skill_name}`.
- Receives `verification_uri_complete`, `user_code`, `expires_in`.
- Opens `verification_uri_complete` in a new tab.
- Shows inline: "Approve in the new tab. Code: **ABCD-EFGH**" with a spinner.
- Polls `GET /oauth/aws-sso/status/{skill_name}?job_id={job_id}` every 5 seconds.

**`needs_selection` state:**
- Renders a dropdown or list of account + role combinations.
- User picks one → `POST /oauth/aws-sso/select/{skill_name}`.

**Authenticated state:**
- Shows connected account name, role, and credential expiry.
- "Revoke" button — calls `POST /oauth/aws-sso/revoke/{skill_name}`, which calls `sso:Logout` with the stored SSO access token to invalidate the session at the provider, then deletes all SM entries for the skill.

No browser redirect callback is involved — the entire flow is handled via polling within the Credentials panel, with no page navigation.

---

### Token Revocation

#### Standard OAuth skills

When a user clicks Revoke on a standard OAuth skill, the backend calls `POST /oauth/revoke/{skill_name}`, which:

1. Loads the SM blob for the skill (`yoloscribe/{user_id}/oauth/{skill_name}`).
2. Reads `auth_server_metadata.revocation_endpoint` from the blob (stored at token-save time during the original OAuth flow).
3. If a revocation endpoint is present:
   - POSTs the access token with `token_type_hint=access_token`.
   - If a refresh token is also stored, POSTs it with `token_type_hint=refresh_token`.
   - Some providers (GitHub) require the `client_secret` to revoke — the blob also contains `client_id` and `client_secret`, so this is always available.
4. Deletes the SM secret regardless of whether the revocation HTTP call succeeded (a network failure should not block the user from disconnecting locally).

**Provider-specific notes:**

| Provider | Revocation endpoint | Notes |
|---|---|---|
| GitHub | `https://api.github.com/applications/{client_id}/token` via `DELETE` | Requires `client_id` + `client_secret` as HTTP Basic auth; not standard RFC 7009 |
| Linear | Advertised in OAuth discovery metadata | Standard RFC 7009 |
| Google Workspace | `https://oauth2.googleapis.com/revoke` | Standard RFC 7009; accepts either token type |

For providers that do not advertise a revocation endpoint and do not have a known fixed URL, the backend skips the provider call, deletes from SM, and the frontend shows: *"Credentials removed from YoloScribe. To fully revoke access, visit [provider] → Settings → Authorized Apps."*

The `revocation_endpoint` and a `revocation_style` hint (`rfc7009` | `github`) should be added to the token blob at save time so the revoke endpoint has everything it needs without re-doing discovery.

#### AWS SSO

`POST /oauth/aws-sso/revoke/{skill_name}`:

1. Loads the SM blob (`yoloscribe/{user_id}/aws-sso/{skill_name}`).
2. Calls `sso:Logout` with the stored `sso_access_token`. This immediately invalidates the SSO session at the IAM Identity Center level — equivalent to `aws sso logout` on the CLI.
3. Deletes the SM blob and the three individual `AWS_SSO_*` secrets.

Note: the short-lived STS role credentials (`accessKeyId` etc.) cannot be individually revoked via API. They will continue to work until they expire naturally (1–12 hours). If immediate revocation of STS credentials is required, an IAM administrator can go to IAM → Roles → the specific role → **Revoke sessions**, which attaches a time-based deny policy — but this affects all holders of that role, not just one YoloScribe user.

#### New backend endpoints

| Endpoint | Skill type | Action |
|---|---|---|
| `POST /oauth/revoke/{skill_name}` | Standard OAuth | Call provider revocation endpoint, delete SM secret |
| `POST /oauth/aws-sso/revoke/{skill_name}` | AWS SSO | Call `sso:Logout`, delete SM blob + individual secrets |

#### IAM policy addition for AWS SSO revocation

The backend IRSA role needs one additional action alongside the existing SSO permissions:

```json
{
  "Effect": "Allow",
  "Action": [
    "sso:Logout"
  ],
  "Resource": "*"
}
```

---

### IAM Policy Changes

The backend service account IAM policy needs two additions:

1. **Platform client cache** — already covered by the existing `secretsmanager:*` on `yoloscribe* if using the same prefix; otherwise add `yoloscribe/platform/aws-sso/*` explicitly.
2. **`sso-oidc` and `sso` API calls** — the SSO OIDC and SSO services are called from the backend web process, not from the agent runner. The backend's IRSA role needs:

```json
{
  "Effect": "Allow",
  "Action": [
    "sso-oidc:RegisterClient",
    "sso-oidc:StartDeviceAuthorization",
    "sso-oidc:CreateToken"
  ],
  "Resource": "*"
},
{
  "Effect": "Allow",
  "Action": [
    "sso:ListAccounts",
    "sso:ListAccountRoles",
    "sso:GetRoleCredentials"
  ],
  "Resource": "*"
}
```

These are all read/auth-only calls on the SSO plane; they do not grant any access to the user's AWS resources directly. The actual AWS access is controlled by the role the user selects during the flow.

The agent runner's per-user IRSA role does **not** need SSO permissions. It reads the already-resolved credentials from Secrets Manager, just as it does today.

---

### Summary of Changes Required

| Area | Change |
|---|---|
| `mcp.json` format | New `auth.type: "aws-sso"` block with `region` and `ssoStartUrl` |
| `main.py` | 3 new AWS SSO endpoints: `initiate`, `status`, `select`; update `get_secrets_status` |
| `main.py` | 2 new revocation endpoints: `POST /oauth/revoke/{skill}`, `POST /oauth/aws-sso/revoke/{skill}` |
| `main.py` | Client registration cache in Secrets Manager |
| `main.py` | Store `revocation_endpoint` + `revocation_style` in OAuth token blob at save time |
| Agent runner | Credential refresh check before job execution |
| Frontend `CredentialsPanel` | New `aws-sso` card type with polling UI; replace "Disconnect" with "Revoke" on all OAuth and AWS SSO skill cards |
| IAM policy (`yoloscribe-backend-policy.json`) | Add `sso-oidc:RegisterClient/StartDeviceAuthorization/CreateToken`, `sso:ListAccounts/ListAccountRoles/GetRoleCredentials/Logout` |
| Secrets Manager | New secret paths for SSO blob and individual credential vars |
