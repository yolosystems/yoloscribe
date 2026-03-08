## Users, Groups, and Modular SSO

This document describes the future implementation plan for groups, Full Control sharing, and modular SSO in AgentScribe.

> **DO NOT IMPLEMENT NOW.** This is a planning document only.

---

### Background

The access control system introduced in spec 07 supports three visibility modes (public, private, shared) and two permission levels for shared users (view, write). A "Full Control" permission level — which would allow shared users to create, edit, and run agents on pages they don't own — is intentionally deferred because it requires changes to the IAM trust model that are not compatible with high-frequency sharing scenarios.

This document describes the path to Full Control sharing via a group model, and the modular SSO system needed to support enterprise identity providers.

---

### The Groups Model

#### Why Groups Are Needed

When an agent runs, it runs as the user visiting the page via an EKS service account annotated with an IAM role. The IAM role's inline policy grants access only to that user's S3 prefix (`{site_name}/*`). For a shared user to have Full Control, their agent runs would need access to the page owner's S3 prefix — but updating IAM policies per-share is not scalable: IAM is not designed for high-frequency policy mutations.

The solution is a **group**: a named entity with its own IAM role and policy. A group's policy covers all S3 prefixes the group has been granted access to. Members of the group assume the group's IAM role when running agents on shared pages.

#### Group Entity Model

```
Group:
  group_id: UUID
  name: string           # human-readable, scoped to the AgentScribe instance
  created_by: user_id
  s3_prefixes: [string]  # list of "{site_name}/*" prefixes the group can access
  members: [user_id]
```

#### IAM Role Provisioning for Groups

When a group is created:
1. An IAM role `agentscribe-group-{group_id}` is created with an IRSA trust policy targeting a Kubernetes service account `group-{group_id}` in the `agentscribe` namespace.
2. An inline policy is attached granting `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on all prefixes in `group.s3_prefixes`.
3. The K8s service account is annotated with the group's IAM role ARN.

When a page is shared with Full Control to a group:
1. The page's S3 prefix is added to the group's `s3_prefixes` list.
2. The group's IAM policy is updated to include the new prefix.

When a user runs an agent on a page they have Full Control access via a group:
1. The agent runner uses the `group-{group_id}` K8s service account instead of the user's personal service account.

**IAM policy update frequency concern:** Policy updates still happen, but at group-level (one update per page-share-to-group) rather than per-user-share. With careful rate limiting and batching (e.g. update the policy at most once per 30 seconds, coalescing concurrent changes), this is tractable.

#### Permission Model with Groups

| Permission Level | Who | Can view | Can edit markdown | Can run agents |
|---|---|---|---|---|
| Public | Anyone | ✓ | — | — |
| Private | Site owner | ✓ | ✓ | ✓ |
| Shared / View | Named user or group member | ✓ | — | — |
| Shared / Write | Named user or group member | ✓ | ✓ | — |
| Shared / Full Control | Group member only | ✓ | ✓ | ✓ |

Individual users (not in a group) can have View or Write access, but never Full Control. Full Control requires group membership.

---

### Storage for Groups (Serverless-First)

#### DynamoDB Table: `agentscribe-groups`

Primary key: `group_id` (UUID, partition key)

Attributes:
- `name` (string)
- `created_by` (string — user_id)
- `s3_prefixes` (string set)
- `members` (string set — user_ids)
- `created_at` (ISO timestamp)

Secondary indexes:
- GSI on `created_by` — to list groups owned by a user
- GSI on `members` (via DynamoDB list flattening or separate membership table) — to look up a user's group memberships

#### Alternative: S3 Tables (Preview)

AWS S3 Tables (based on Apache Iceberg) provide a serverless tabular store. This could replace DynamoDB for groups if the query patterns are compatible (point lookups + simple scans). S3 Tables add complexity in SDK maturity; DynamoDB is the pragmatic choice for initial implementation.

#### DynamoDB Table: `agentscribe-group-memberships`

Separate table for efficient reverse lookups (user → groups):

- Partition key: `user_id`
- Sort key: `group_id`
- Attributes: `joined_at`

This table is kept in sync with `agentscribe-groups.members` by the group management API.

---

### Modular SSO

#### Current State

AgentScribe is tightly coupled to Supabase for authentication. Supabase handles:
1. OAuth2/OIDC flows (Google, GitHub, etc.)
2. JWT issuance and JWKS endpoint
3. User metadata (email, `app_metadata.site_name`)
4. User table and PostgREST API

#### Target Architecture

Replace the Supabase-specific code with a modular `AuthProvider` abstraction. Each provider implements:

```python
class AuthProvider(Protocol):
    def get_jwks_uri(self) -> str: ...
    def decode_claims(self, token: str) -> JWTClaims: ...
    def get_user_site(self, user_id: str) -> str | None: ...
    def set_user_site(self, user_id: str, site_name: str) -> None: ...
    def delete_user(self, user_id: str) -> None: ...
```

Concrete providers:
- `SupabaseAuthProvider` — current implementation, extracted and wrapped
- `GenericOIDCProvider` — any OAuth2/OIDC-compliant IdP (Keycloak, Auth0, Okta, Cognito, etc.)
- `SAMLProvider` — SAML 2.0 with SP-initiated SSO (see below)

The active provider is selected by a new `AUTH_PROVIDER` environment variable (`supabase`, `oidc`, `saml`), with provider-specific configuration via additional env vars.

#### Supported SSO Protocols

| Protocol | Standard | Group Support | Examples |
|---|---|---|---|
| OAuth2 + OIDC | RFC 6749 / OpenID Connect 1.0 | Via `groups` claim (non-standard but common) | Google, GitHub, Okta, Cognito, Keycloak |
| SAML 2.0 | OASIS SAML 2.0 | Via `AttributeStatement` with group attributes | Microsoft Entra ID (Azure AD), ADFS, Okta, PingIdentity |
| SCIM 2.0 | RFC 7643/7644 | Provisioning-time group sync | Okta, Azure AD, Workday |
| LDAP / Active Directory | RFC 4511 | `memberOf` attribute | AD, OpenLDAP (via proxy/federation) |

**Recommended initial additions:** SAML 2.0 (for enterprise) and a generic OIDC provider (for Okta, Cognito, Keycloak).

#### SAML 2.0 Integration

SAML flow for AgentScribe:
1. User visits an AgentScribe site and clicks "Sign in with SSO".
2. AgentScribe acts as the Service Provider (SP), redirects to the Identity Provider (IdP).
3. IdP authenticates the user and posts a SAML assertion to `/auth/saml/callback`.
4. Backend validates the assertion (signature, audience, expiry) using the IdP's certificate.
5. Backend extracts `NameID` (email), `groups` attribute, and any other claims.
6. Backend issues its own short-lived JWT (or stores a session) and redirects to the frontend.

Group mapping from SAML:
- `AttributeStatement` typically contains a `groups` attribute with one or more `AttributeValue` entries listing the user's group memberships.
- AgentScribe reads these groups at login time and auto-creates/syncs corresponding AgentScribe groups.
- Users are added to AgentScribe groups matching their SAML group names; group IAM roles are provisioned lazily on first Full Control share.

Libraries: `python3-saml` or `pysaml2` for SP-side SAML validation.

#### Auto-Provisioning Groups from SAML Claims

On SAML login:
1. Parse the `groups` claim from the SAML assertion.
2. For each group name, upsert a row in `agentscribe-groups` (keyed by `name`, scoped to the OIDC/SAML tenant).
3. Add the user to those groups in `agentscribe-group-memberships`.
4. Remove the user from any AgentScribe groups whose SAML group name is no longer in the assertion (group membership is authoritative from the IdP).

#### OIDC Groups Claim

Several OIDC providers support a `groups` claim in the ID token or userinfo endpoint:
- Keycloak: configurable via Client Scope
- Okta: configurable in the OIDC application
- Azure AD: `groups` claim (object IDs) or `groupMembershipClaims`
- Cognito: via `cognito:groups` custom claim

AgentScribe reads the `groups` claim (array of strings) at login time and applies the same auto-provisioning logic as SAML.

---

### Implementation Phases

#### Phase 1: Modular Auth Provider
- Extract Supabase-specific code into `SupabaseAuthProvider`
- Define `AuthProvider` protocol + `JWTClaims` dataclass
- Add `GenericOIDCProvider` (any OIDC-compatible IdP)
- Configuration via `AUTH_PROVIDER` env var

#### Phase 2: SAML 2.0 Support
- Add `SAMLProvider` with SP metadata generation (`/auth/saml/metadata`)
- SAML assertion validation at `/auth/saml/callback`
- Session management (short-lived signed JWT issued by AgentScribe)
- Group claim extraction and mapping

#### Phase 3: Groups Data Model
- DynamoDB tables `agentscribe-groups` and `agentscribe-group-memberships`
- Group CRUD API (create, list, add/remove members)
- Auto-provisioning from SAML/OIDC `groups` claim at login

#### Phase 4: Group IAM Roles
- IAM role + K8s service account provisioned at group creation
- IAM policy updated when pages are shared with Full Control to a group
- Agent runner selects service account based on access level and group membership

#### Phase 5: Full Control Sharing in Frontend
- `PageSettingsPanel` extended with "Full Control" access level (group members only)
- Group picker when sharing with Full Control
- Agent runner job sends group_id alongside user_id so the worker selects the correct service account
