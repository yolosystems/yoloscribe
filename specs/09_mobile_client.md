## Mobile Client

This document describes the design for a native mobile client (iOS and Android) for AgentScribe using the existing FastAPI backend without modification.

> **DO NOT IMPLEMENT NOW.** This is a planning document only.

---

### Overview

The web frontend is a single-page application served from S3 with hash-based routing and Supabase auth. A native mobile app replaces the React SPA with a native UI while talking to the same API endpoints. No backend changes are required — the backend already exposes all necessary primitives.

The mobile app is the functional peer of the web client: read pages, edit pages, chat with AI agents, manage page settings, and configure credentials. The native form factor enables push notifications (replacing the polling-based notification bell), offline content caching, and a more natural reading/editing experience.

---

### Authentication

#### Supabase OAuth on Mobile

The web client uses Supabase's browser-based Google OAuth flow. On mobile the same flow is implemented using the platform's secure browser (ASWebAuthenticationSession on iOS, Custom Tabs on Android) rather than a WebView.

Flow:
1. App calls Supabase to obtain an authorization URL for the Google OAuth provider.
2. Platform secure browser opens. User authenticates with Google.
3. Supabase redirects to the app via a registered deep link scheme (e.g. `agentscribe://auth/callback`).
4. App extracts the tokens from the URL fragment and stores them in the platform keychain (iOS Keychain / Android Keystore).
5. Supabase client SDK on mobile handles automatic token refresh. The raw `access_token` (a JWT) is attached to every API request as `Authorization: Bearer <token>`.

The backend is stateless with respect to auth — it validates the JWT against Supabase's JWKS endpoint on every request. No session state is held server-side, so mobile and web sessions are fully independent and can coexist simultaneously.

#### Sign-out

Clear tokens from the keychain and call Supabase's sign-out endpoint to invalidate the session server-side.

---

### Site and Page Routing

The web frontend derives the site name from the first URL path segment and navigates within a site using the URL hash. Mobile replaces both with in-app state.

#### Site Name

On first launch after sign-in, the app calls `GET /my-site` to determine whether the user has provisioned a site:
- **No site yet** → navigate to the Onboarding screen.
- **Site exists** → store `site_name` in app state; navigate to the Home screen (root page of the site).

The site name is stored in memory for the session and persisted locally (e.g. in `UserDefaults` / `SharedPreferences`) so the app can skip the `/my-site` call on subsequent launches when a cached value exists. The cached value is invalidated on sign-out.

#### Page Navigation

The hash-based routing of the web client maps directly to a navigation stack on mobile:

| Web hash | Mobile concept |
|---|---|
| (empty) | Root page — stack root |
| `#/{page}` | Child page — push onto stack |
| `#/{page}/{sub}` | Grandchild page — push again |
| `#/.agents/{name}` | Agent definition view — modal or push |
| `#/.user/notifications` | Notifications screen — push |
| `#/.user/search` | Search screen — dedicated tab or push |

The app maintains a navigation stack. Navigating into a child page pushes a new screen; the system back gesture / back button pops it. The breadcrumb trail visible in the web client maps to the native navigation bar title and back stack.

Internal links in rendered markdown (e.g. `[notes](/knuth-home/projects)`) are intercepted by the in-app link handler, which strips the site prefix and translates relative paths into navigation stack pushes rather than opening a browser.

---

### Content Loading and Display

#### Fetching Content

`GET /content?site={site}&path={filePath}` is the primary content endpoint. The app reads the `X-Page-Access` response header to determine the access level:
- `full-control` — owner; all edit, chat, and settings controls visible.
- `write` — shared write user; editor visible, no chat or agent controls.
- `view` — shared view or public; read-only.
- 403 response — trigger the Access Denied screen.

#### Rendering

Page content is Markdown. On mobile this is rendered using a native Markdown renderer rather than `react-markdown`:
- **iOS**: `swift-markdown-ui` or equivalent.
- **Android**: `Markwon` library.

Both support GFM (tables, strikethrough, task lists). The rendering surface must handle inline links via the in-app link interceptor described above.

#### Caching

Content is cached on-device keyed by `(site, filePath)`. The cache is:
- Written on successful `GET /content`.
- Read immediately to show cached content while the network request is in flight (stale-while-revalidate).
- Cleared on `PUT /content` success for the affected path.
- Invalidated on sign-out.

This enables basic offline reading of previously visited pages.

---

### Editing

#### Edit Mode

Edit mode is entered via a toolbar button visible when `accessLevel` is `full-control` or `write`. The content switches from the rendered Markdown view to a plain-text editor with monospace font. A live preview pane can be offered as a split view on iPad / tablets with sufficient screen width.

#### Saving

`PUT /content?site={site}&path={filePath}` with the content as the raw request body (`Content-Type: text/plain`). The app tracks dirty state to gate the Save button. Autosave is not implemented — explicit save is required.

On save success, the cache entry for the path is updated and the view returns to read mode.

#### Discard

A discard button (or standard pull-down-to-dismiss on iOS) prompts confirmation if dirty, then reverts to the last saved content.

---

### Chat Panel

The chat interface is the primary AI interaction surface. On mobile it is presented as a full-screen sheet (slide up from bottom) rather than a side panel.

#### Request/Response

`POST /chat` with JSON body:
```json
{
  "message": "user message",
  "current_content": "current page markdown",
  "history": [{ "role": "user|assistant", "content": "..." }],
  "site": "site-name",
  "file_path": "content.md"
}
```

Response:
```json
{
  "reply": "assistant text",
  "updated_content": "new markdown or null",
  "navigate_to": "#/page or null"
}
```

The chat endpoint is not streaming; the app shows a typing indicator (animated dots) until the response arrives. On slow connections a timeout of 120 s is recommended given that agent reasoning chains can be long.

#### History

Conversation history for the current page session is maintained in memory and sent with each request as the `history` array, matching the web client's behaviour. History is not persisted across app launches — each session starts fresh.

#### Content Updates

If `updated_content` is non-null and differs from the current content, the app applies the update to the editor buffer (or the displayed content if not in edit mode) and syncs to disk via `PUT /content`. The user sees a diff-style highlight or an inline notification that the agent updated the page.

#### Navigation

If `navigate_to` is non-null (e.g. `#/new-page`), the app converts the hash to a page path and pushes the new screen onto the navigation stack, closing the chat sheet.

---

### Page Management

#### Listing Child Pages

`GET /pages?site={site}&page_path={pagePath}` returns `{ "pages": ["child1", "child2"] }`. Child page names are displayed as a list section below the rendered content on each page view, matching the `ChildPagesList` component in the web client.

#### Creating Pages

Owners can create child pages from a "New Page" action (toolbar button or FAB). A sheet prompts for a URL-slug-style name (lowercase, hyphens, underscores). On confirm, the app calls `POST /pages` with `{ site, page_path }` and navigates to the new page on success.

---

### Agents

`GET /agents?site={site}&page_path={pagePath}` returns the list of agent names for the current page. These are shown as a section within the chat sheet (or a separate "Agents" tab within it) when the user is the site owner. Tapping an agent name loads its definition file (`{pagePath}/.agents/{name}/agent.md`) via `GET /content` and displays it in a read-only view. Agent creation is delegated to the chat agent (ask the assistant to create an agent) rather than exposed as a direct UI flow.

---

### Settings and Visibility

A "Page Settings" screen (accessible via a toolbar action, owner-only) mirrors the `PageSettingsPanel` component.

`GET /settings?site={site}&path={filePath}` loads current settings. `PUT /settings` saves them.

The screen provides:
- A segmented control for visibility: **Private / Shared / Public**.
- A list of shared users (email + access level picker: View / Write) when visibility is Shared.
- Add user: email text field + access level picker + Add button.
- Remove user: swipe-to-delete or trash button per row.

Changes are not auto-saved; an explicit Save button calls `PUT /settings`.

---

### Access Requests

When `GET /content` returns 403, the app shows an Access Denied screen with:
- If not signed in: a sign-in button.
- If signed in: a "Request Access" button that calls `POST /request-access` with `{ site, path }` and shows a confirmation toast.

---

### Credentials (Secrets)

A "Credentials" screen (accessible from a settings or profile area, owner-only) maps to the `CredentialsPanel` component.

`GET /secrets/status` returns all skills and their credential state. The screen renders:

**OAuth skills** (`type: "oauth"`):
- Authenticated badge with expiry date, or unauthenticated state.
- "Connect" / "Reconnect" button → calls `POST /oauth/initiate/{skillName}` → receives `{ auth_url }` → opens the URL in the platform secure browser → handles deep-link callback (`agentscribe://oauth/callback?oauth_success=…` or `?oauth_error=…`) → updates the UI.

**Key-based skills** (`type: "key"`):
- List of required variables with stored/missing status.
- Tapping a variable opens a secure text entry sheet → on save calls `PUT /secrets/{varName}` with `{ value }`.

---

### Notifications

The web client polls `.user/notifications.md` on a timer and shows a red badge on a bell icon. Mobile replaces polling with push notifications.

On the backend side (a future addition) a Lambda or worker process watches for writes to `{site}/.user/notifications.md` via S3 Event Notifications and sends a push notification via APNs / FCM. The mobile app registers its device token on sign-in and receives pushes without keeping a persistent connection open.

In the absence of push infrastructure, the app falls back to fetching `.user/notifications.md` via `GET /content?path=.user/notifications.md` on app foreground. If the content is non-empty, a badge is shown on the Notifications tab. The Notifications screen renders the file as Markdown (a list of timestamped access-request entries).

---

### Onboarding

If `GET /my-site` returns `{ site_name: null }`, the user has no site yet. The Onboarding screen prompts for:
- **Site name** — validated client-side to match `^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$`.
- **Theme** — a card picker showing mini previews of the three themes (dark, light, yolo).

On confirm, `POST /provision` is called with `{ site_name, theme }`. On success the app navigates to the new site's root page.

---

### Theming

`GET /content?path=config.json` returns `{ "theme": "dark" | "light" | "yolo" }`. The app applies the corresponding theme (system colour palette + accent colours) on launch and on returning to the foreground. The three themes match the web client's CSS variable definitions.

---

### Screen Map

```
Launch
├── Loading (checking auth + /my-site)
├── Sign-in screen (no session)
├── Onboarding (session + no site)
└── Main Tab Bar
    ├── Home (root page of site)
    │   ├── Page View
    │   │   ├── Rendered Markdown
    │   │   ├── Child Pages List
    │   │   ├── [Edit] → Editor Screen
    │   │   │   └── Chat Sheet (owner only)
    │   │   ├── [Settings] → Page Settings Screen (owner only)
    │   │   └── [New Page] → New Page Sheet (owner only)
    │   └── Child Page View (recursive)
    ├── Search (owner only; .user/search.md)
    ├── Notifications (owner only; .user/notifications.md)
    └── Profile
        ├── Credentials Screen (owner only)
        ├── Theme picker
        └── Delete Account (confirmation modal)
```

---

### API Compatibility

The mobile client uses the existing FastAPI backend with zero backend changes. Every API call the mobile app makes is already supported:

| Endpoint | Mobile use |
|---|---|
| `GET /content` | Load any page/file |
| `PUT /content` | Save edits |
| `POST /chat` | Chat panel |
| `GET /pages` | Child page list |
| `POST /pages` | Create page |
| `GET /agents` | Agent list |
| `GET /settings` | Page settings |
| `PUT /settings` | Save page settings |
| `POST /request-access` | Access denied screen |
| `GET /secrets/status` | Credentials screen |
| `PUT /secrets/{var}` | Save credential |
| `POST /oauth/initiate/{skill}` | OAuth connect |
| `GET /oauth/callback` | OAuth deep-link callback |
| `GET /my-site` | Onboarding check |
| `POST /provision` | Onboarding |
| `DELETE /account` | Profile screen |

---

### Implementation Notes

- **Recommended stack**: React Native with Expo (shares some logic with the web frontend — in particular the API layer and Supabase client configuration) is the lowest-effort path. Alternatively, Swift/SwiftUI + Kotlin/Compose if native performance is a priority.
- **Deep link scheme**: Register `agentscribe://` for OAuth and auth callbacks on both platforms.
- **Secure storage**: Always use the platform keychain/keystore for tokens and secrets — never `AsyncStorage` or `SharedPreferences` for sensitive values.
- **Network timeout**: Set a 120 s timeout on `POST /chat` to accommodate long reasoning chains; use a shorter timeout (10 s) for all other endpoints.
- **Offline**: Implement read-only offline mode via the content cache. Write operations fail gracefully with a toast when offline.
