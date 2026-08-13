/**
 * Generic OIDC auth client — authorization code + PKCE, entirely in the browser.
 *
 * Works against any OIDC-compliant provider (Auth0, Keycloak, Okta, Cognito,
 * Entra, …). Endpoints are read from the provider's discovery document rather
 * than hardcoded, so switching IdP is a config change:
 *
 *   VITE_OIDC_CONFIG_URL  — .well-known/openid-configuration URL
 *   VITE_OIDC_CLIENT_ID   — public client ID (no client_secret; see below)
 *   VITE_OIDC_SCOPE       — optional; default 'openid email profile offline_access'
 *   VITE_OIDC_TOKEN       — optional; 'id' (default) or 'access' — which token to
 *                           send to the YoloScribe backend as the bearer
 *
 * POLICY: this is a *public* client. It holds no client_secret — a browser
 * cannot keep one — which is why PKCE is mandatory rather than optional here.
 *
 * Token storage: localStorage under STORAGE_KEY.
 * PKCE verifier + CSRF state: sessionStorage (cleared after exchange).
 *
 * Flow:
 *   signIn()       → discover → PKCE pair + state → redirect to authorization_endpoint
 *   on callback    → validate state → exchange ?code= for tokens → store → notify
 *   onLoad/refresh → restore from localStorage; renew via refresh_token if near expiry
 *   signOut()      → clear storage → notify → redirect to end_session_endpoint if published
 */

import type { AuthClient, AuthSession } from './auth'

const STORAGE_KEY = 'ys_oidc_session'
const VERIFIER_KEY = 'ys_oidc_verifier'
const STATE_KEY = 'ys_oidc_state'

/** Renew this many seconds before the bearer token actually expires. */
const RENEW_SKEW_SECONDS = 120

interface StoredSession {
  access_token: string      // the bearer sent to the YoloScribe backend
  id_token: string
  refresh_token?: string
  expires_at: number        // Unix timestamp (seconds)
  user_id: string
  email?: string
  full_name?: string
}

interface Discovery {
  authorization_endpoint: string
  token_endpoint: string
  end_session_endpoint?: string
}

export interface OidcConfig {
  configUrl: string
  clientId: string
  redirectUri: string
  scope?: string
  /** Which token to use as the backend bearer. Default 'id'. */
  bearer?: 'id' | 'access'
}

interface TokenResponse {
  access_token: string
  id_token?: string
  refresh_token?: string
  expires_in?: number
}

// ── PKCE / state helpers ──────────────────────────────────────────────────────

function randomUrlSafe(byteLength: number): string {
  const bytes = new Uint8Array(byteLength)
  crypto.getRandomValues(bytes)
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '')
}

async function generateChallenge(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
  return btoa(String.fromCharCode(...new Uint8Array(digest)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '')
}

// ── JWT decode (no verification — signatures are validated server-side) ───────

function decodeJwtPayload(token: string): Record<string, unknown> {
  try {
    const part = token.split('.')[1]
    const json = atob(part.replace(/-/g, '+').replace(/_/g, '/'))
    return JSON.parse(json)
  } catch {
    return {}
  }
}

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000)
}

// ── OidcAuthClient ────────────────────────────────────────────────────────────

export class OidcAuthClient implements AuthClient {
  private readonly _config: OidcConfig
  private _session: AuthSession | null = null
  private _subscribers: Array<(session: AuthSession | null) => void> = []
  private _discovery: Promise<Discovery> | null = null
  private _initialized: Promise<void> | null = null
  private _renewTimer: ReturnType<typeof setTimeout> | null = null

  constructor(config: OidcConfig) {
    this._config = config
  }

  onAuthStateChange(callback: (session: AuthSession | null) => void): () => void {
    this._subscribers.push(callback)

    // Initialization runs once no matter how many subscribers attach.
    this._initialized ??= this._initialize()
    void this._initialized.then(() => callback(this._session))

    return () => {
      this._subscribers = this._subscribers.filter((s) => s !== callback)
    }
  }

  signIn(): void {
    void (async () => {
      const { authorization_endpoint } = await this._discover()

      // A fresh verifier and state per attempt — reusing either across attempts
      // would weaken both the PKCE binding and the CSRF check.
      const verifier = randomUrlSafe(48)
      const state = randomUrlSafe(16)
      sessionStorage.setItem(VERIFIER_KEY, verifier)
      sessionStorage.setItem(STATE_KEY, state)

      const params = new URLSearchParams({
        response_type: 'code',
        client_id: this._config.clientId,
        redirect_uri: this._config.redirectUri,
        code_challenge: await generateChallenge(verifier),
        code_challenge_method: 'S256',
        scope: this._config.scope ?? 'openid email profile offline_access',
        state,
      })
      window.location.href = `${authorization_endpoint}?${params}`
    })()
  }

  async signOut(): Promise<void> {
    const idToken = this._loadSession()?.id_token
    this._clearSession()
    this._notify(null)

    // Only redirect if the provider publishes RP-initiated logout; otherwise the
    // local session is cleared and the IdP session simply stays alive.
    try {
      const { end_session_endpoint } = await this._discover()
      if (!end_session_endpoint) return
      const params = new URLSearchParams({
        client_id: this._config.clientId,
        post_logout_redirect_uri: this._config.redirectUri,
      })
      if (idToken) params.set('id_token_hint', idToken)
      window.location.href = `${end_session_endpoint}?${params}`
    } catch {
      // Discovery unreachable — the local sign-out above already took effect.
    }
  }

  // ── Private ────────────────────────────────────────────────────────────────

  private async _discover(): Promise<Discovery> {
    this._discovery ??= (async () => {
      const resp = await fetch(this._config.configUrl)
      if (!resp.ok) {
        throw new Error(`OIDC discovery failed: ${resp.status} ${this._config.configUrl}`)
      }
      const doc = await resp.json()
      if (!doc.authorization_endpoint || !doc.token_endpoint) {
        throw new Error('OIDC discovery document is missing required endpoints')
      }
      return doc as Discovery
    })().catch((err) => {
      this._discovery = null   // don't cache a failure — allow a later retry
      throw err
    })
    return this._discovery
  }

  private async _initialize(): Promise<void> {
    const url = new URL(window.location.href)
    const code = url.searchParams.get('code')
    const returnedState = url.searchParams.get('state')

    if (code) {
      const expectedState = sessionStorage.getItem(STATE_KEY)
      // Reject a callback whose state doesn't match the one we issued — this is
      // the CSRF check, so a mismatch must abort rather than proceed.
      if (!expectedState || expectedState !== returnedState) {
        sessionStorage.removeItem(VERIFIER_KEY)
        sessionStorage.removeItem(STATE_KEY)
        this._stripAuthParams()
        return
      }
      await this._exchangeCode(code)
      this._stripAuthParams()
      return
    }

    // Provider returned an error instead of a code (e.g. access_denied).
    if (url.searchParams.get('error')) {
      this._stripAuthParams()
      return
    }

    const stored = this._loadSession()
    if (!stored) return

    if (stored.expires_at - RENEW_SKEW_SECONDS <= nowSeconds()) {
      await this._refresh(stored)
      return
    }

    this._session = toAuthSession(stored)
    this._scheduleRenew(stored)
  }

  /** Remove OAuth callback params without a reload, preserving path and hash. */
  private _stripAuthParams(): void {
    const url = new URL(window.location.href)
    for (const p of ['code', 'state', 'error', 'error_description', 'session_state', 'iss']) {
      url.searchParams.delete(p)
    }
    const clean = url.pathname + (url.search === '?' ? '' : url.search) + url.hash
    window.history.replaceState({}, '', clean)
  }

  private async _exchangeCode(code: string): Promise<void> {
    const verifier = sessionStorage.getItem(VERIFIER_KEY)
    if (!verifier) return  // verifier lost (e.g. callback opened in another tab)

    try {
      const { token_endpoint } = await this._discover()
      const resp = await fetch(token_endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          grant_type: 'authorization_code',
          code,
          redirect_uri: this._config.redirectUri,
          client_id: this._config.clientId,
          code_verifier: verifier,
        }).toString(),
      })
      if (!resp.ok) return
      this._storeSession(await resp.json())
    } catch {
      // Exchange failed; stay signed out.
    } finally {
      sessionStorage.removeItem(VERIFIER_KEY)
      sessionStorage.removeItem(STATE_KEY)
    }
  }

  /**
   * Renew with the refresh token. On any failure the session is cleared and
   * subscribers are notified, so the UI falls back to signed-out rather than
   * silently holding a token the backend will reject.
   */
  private async _refresh(stored: StoredSession): Promise<void> {
    if (!stored.refresh_token) {
      this._clearSession()
      this._notify(null)
      return
    }
    try {
      const { token_endpoint } = await this._discover()
      const resp = await fetch(token_endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          grant_type: 'refresh_token',
          refresh_token: stored.refresh_token,
          client_id: this._config.clientId,
        }).toString(),
      })
      if (!resp.ok) {
        this._clearSession()
        this._notify(null)
        return
      }
      const data: TokenResponse = await resp.json()
      // Providers that rotate refresh tokens omit nothing; those that don't
      // return one expect the existing token to be reused.
      this._storeSession({ ...data, refresh_token: data.refresh_token ?? stored.refresh_token })
      this._notify(this._session)
    } catch {
      this._clearSession()
      this._notify(null)
    }
  }

  private _storeSession(data: TokenResponse): void {
    const idToken = data.id_token ?? data.access_token
    const bearer = this._config.bearer === 'access' ? data.access_token : idToken
    const claims = decodeJwtPayload(idToken)

    // Prefer the bearer's own exp claim over expires_in: when the ID token is the
    // bearer, expires_in describes the *access* token and can be the wrong value.
    const bearerExp = decodeJwtPayload(bearer)['exp']
    const expiresAt = typeof bearerExp === 'number'
      ? bearerExp
      : nowSeconds() + (data.expires_in ?? 3600)

    const stored: StoredSession = {
      access_token: bearer,
      id_token: idToken,
      refresh_token: data.refresh_token,
      expires_at: expiresAt,
      user_id: (claims['sub'] as string) ?? '',
      email: claims['email'] as string | undefined,
      full_name: (claims['name'] ?? claims['preferred_username']) as string | undefined,
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(stored))
    this._session = toAuthSession(stored)
    this._scheduleRenew(stored)
  }

  private _loadSession(): StoredSession | null {
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      return raw ? (JSON.parse(raw) as StoredSession) : null
    } catch {
      return null
    }
  }

  private _clearSession(): void {
    if (this._renewTimer) {
      clearTimeout(this._renewTimer)
      this._renewTimer = null
    }
    localStorage.removeItem(STORAGE_KEY)
    sessionStorage.removeItem(VERIFIER_KEY)
    sessionStorage.removeItem(STATE_KEY)
    this._session = null
  }

  private _scheduleRenew(stored: StoredSession): void {
    if (this._renewTimer) clearTimeout(this._renewTimer)
    if (!stored.refresh_token) return
    const delayMs = Math.max(0, (stored.expires_at - RENEW_SKEW_SECONDS - nowSeconds()) * 1000)
    this._renewTimer = setTimeout(() => { void this._refresh(stored) }, delayMs)
  }

  private _notify(session: AuthSession | null): void {
    for (const cb of this._subscribers) cb(session)
  }
}

function toAuthSession(stored: StoredSession): AuthSession {
  return {
    access_token: stored.access_token,
    user: {
      id: stored.user_id,
      email: stored.email,
      user_metadata: stored.full_name ? { full_name: stored.full_name } : undefined,
    },
  }
}
