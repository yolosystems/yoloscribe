/**
 * Cognito auth client — PKCE authorization code flow entirely in the browser.
 *
 * Assumes a public Cognito app client (no client_secret). Uses:
 *   VITE_COGNITO_CLIENT_ID  — Cognito app client ID
 *   VITE_COGNITO_DOMAIN     — Cognito Hosted UI domain (e.g. https://your-pool.auth.us-east-1.amazoncognito.com)
 *
 * Token storage: localStorage under the STORAGE_KEY prefix.
 * PKCE verifier: sessionStorage (cleared after exchange).
 *
 * Flow:
 *   signIn()       → generate PKCE pair → store verifier → redirect to Hosted UI
 *   on callback    → detect ?code= in URL → exchange for tokens → store → notify subscribers
 *   onLoad/refresh → restore session from localStorage → notify subscribers
 *   signOut()      → clear storage → notify subscribers → redirect to Cognito logout
 */

import type { AuthClient, AuthSession } from './auth'

const STORAGE_KEY = 'ys_cognito_session'
const VERIFIER_KEY = 'ys_cognito_verifier'

interface StoredSession {
  access_token: string
  id_token: string
  refresh_token?: string
  expires_at: number  // Unix timestamp (seconds)
  user_id: string
  email?: string
  full_name?: string
}

interface CognitoConfig {
  clientId: string
  domain: string
  redirectUri: string
}

// ── PKCE helpers ──────────────────────────────────────────────────────────────

function generateVerifier(): string {
  const bytes = new Uint8Array(48)
  crypto.getRandomValues(bytes)
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '')
}

async function generateChallenge(verifier: string): Promise<string> {
  const data = new TextEncoder().encode(verifier)
  const digest = await crypto.subtle.digest('SHA-256', data)
  return btoa(String.fromCharCode(...new Uint8Array(digest)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '')
}

// ── JWT decode (no verification — signature validated server-side) ─────────────

function decodeJwtPayload(token: string): Record<string, unknown> {
  try {
    const part = token.split('.')[1]
    return JSON.parse(atob(part.replace(/-/g, '+').replace(/_/g, '/')))
  } catch {
    return {}
  }
}

// ── CognitoAuthClient ─────────────────────────────────────────────────────────

export class CognitoAuthClient implements AuthClient {
  private readonly _config: CognitoConfig
  private _session: AuthSession | null = null
  private _subscribers: Array<(session: AuthSession | null) => void> = []

  constructor(config: CognitoConfig) {
    this._config = config
  }

  onAuthStateChange(callback: (session: AuthSession | null) => void): () => void {
    this._subscribers.push(callback)

    // Kick off initialization async; callback fires once the result is known.
    this._initialize().then(() => callback(this._session))

    return () => {
      this._subscribers = this._subscribers.filter((s) => s !== callback)
    }
  }

  signIn(): void {
    generateChallenge(this._getOrCreateVerifier()).then((challenge) => {
      const params = new URLSearchParams({
        response_type: 'code',
        client_id: this._config.clientId,
        redirect_uri: this._config.redirectUri,
        code_challenge: challenge,
        code_challenge_method: 'S256',
        scope: 'openid email profile',
      })
      window.location.href = `${this._config.domain}/oauth2/authorize?${params}`
    })
  }

  async signOut(): Promise<void> {
    localStorage.removeItem(STORAGE_KEY)
    sessionStorage.removeItem(VERIFIER_KEY)
    this._session = null
    this._notify(null)
    // Redirect to Cognito logout so the hosted-UI session is also cleared.
    const params = new URLSearchParams({
      client_id: this._config.clientId,
      logout_uri: this._config.redirectUri,
    })
    window.location.href = `${this._config.domain}/logout?${params}`
  }

  // ── Private ────────────────────────────────────────────────────────────────

  private async _initialize(): Promise<void> {
    // 1. Check URL for an authorization code (post-Cognito redirect).
    const urlParams = new URLSearchParams(window.location.search)
    const code = urlParams.get('code')
    if (code) {
      await this._exchangeCode(code)
      // Clean the code out of the URL without a full reload.
      const clean = window.location.pathname + window.location.hash
      window.history.replaceState({}, '', clean)
      return
    }

    // 2. Restore session from localStorage.
    const stored = this._loadSession()
    if (stored) {
      this._session = this._toAuthSession(stored)
    }
  }

  private _getOrCreateVerifier(): string {
    let verifier = sessionStorage.getItem(VERIFIER_KEY)
    if (!verifier) {
      verifier = generateVerifier()
      sessionStorage.setItem(VERIFIER_KEY, verifier)
    }
    return verifier
  }

  private async _exchangeCode(code: string): Promise<void> {
    const verifier = sessionStorage.getItem(VERIFIER_KEY)
    if (!verifier) return  // verifier lost (e.g. opened in a different tab) — ignore

    try {
      const body = new URLSearchParams({
        grant_type: 'authorization_code',
        code,
        redirect_uri: this._config.redirectUri,
        client_id: this._config.clientId,
        code_verifier: verifier,
      })
      const resp = await fetch(`${this._config.domain}/oauth2/token`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: body.toString(),
      })
      if (!resp.ok) return

      const data = await resp.json()
      sessionStorage.removeItem(VERIFIER_KEY)
      this._storeSession(data)
    } catch {
      // Exchange failed; stay signed out.
    }
  }

  private _storeSession(data: {
    access_token: string
    id_token?: string
    refresh_token?: string
    expires_in?: number
  }): void {
    const idToken = data.id_token ?? data.access_token
    const claims = decodeJwtPayload(idToken)
    const stored: StoredSession = {
      access_token: idToken,
      id_token: idToken,
      refresh_token: data.refresh_token,
      expires_at: Math.floor(Date.now() / 1000) + (data.expires_in ?? 3600),
      user_id: (claims['sub'] as string) ?? '',
      email: claims['email'] as string | undefined,
      full_name: (claims['name'] ?? claims['cognito:username']) as string | undefined,
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(stored))
    this._session = this._toAuthSession(stored)
  }

  private _loadSession(): StoredSession | null {
    try {
      const raw = localStorage.getItem(STORAGE_KEY)
      if (!raw) return null
      const stored: StoredSession = JSON.parse(raw)
      // Drop expired sessions (with a 60s buffer).
      if (stored.expires_at < Math.floor(Date.now() / 1000) - 60) {
        localStorage.removeItem(STORAGE_KEY)
        return null
      }
      return stored
    } catch {
      return null
    }
  }

  private _toAuthSession(stored: StoredSession): AuthSession {
    return {
      access_token: stored.access_token,
      user: {
        id: stored.user_id,
        email: stored.email,
        user_metadata: stored.full_name ? { full_name: stored.full_name } : undefined,
      },
    }
  }

  private _notify(session: AuthSession | null): void {
    for (const cb of this._subscribers) cb(session)
  }
}
