/**
 * Provider-agnostic auth client.
 *
 * Selected at build time via VITE_AUTH_PROVIDER=supabase|oidc (default: supabase).
 * Both implementations expose the same AuthClient interface so App.tsx has no
 * provider-specific logic.
 *
 * `oidc` is discovery-driven and works against any OIDC-compliant provider —
 * Auth0, Keycloak, Okta, Cognito, Entra. There is no separate Cognito client:
 * Cognito publishes a standard discovery document, so it is configured through
 * `oidc` like any other provider.
 */

import { createClient } from '@supabase/supabase-js'
import { OidcAuthClient } from './auth_oidc'

// ── Types ─────────────────────────────────────────────────────────────────────

export interface AuthSession {
  access_token: string
  user: {
    id: string
    email: string | undefined
    user_metadata?: { full_name?: string }
  }
}

export interface AuthClient {
  /** Subscribe to auth state changes. Returns an unsubscribe function. */
  onAuthStateChange(callback: (session: AuthSession | null) => void): () => void
  /** Initiate sign-in (redirect or popup depending on provider). */
  signIn(): void
  /** Sign out and clear session state. */
  signOut(): Promise<void>
}

// ── Factory ───────────────────────────────────────────────────────────────────

const LOCAL_MODE = import.meta.env.VITE_LOCAL_MODE === 'true'
const AUTH_PROVIDER = import.meta.env.VITE_AUTH_PROVIDER ?? 'supabase'

// Raw Supabase client, exposed for Supabase-specific flows that the provider-
// agnostic AuthClient interface doesn't cover — currently the OAuth 2.1 consent
// screen (`supabaseClient.auth.oauth.*`). null when the provider isn't Supabase.
export const supabaseClient =
  !LOCAL_MODE && AUTH_PROVIDER === 'supabase'
    ? createClient(
        import.meta.env.VITE_SUPABASE_URL as string,
        import.meta.env.VITE_SUPABASE_ANON_KEY as string,
        { auth: { flowType: 'implicit' } },
      )
    : null

function createAuthClient(): AuthClient {
  // In LOCAL_MODE auth is bypassed entirely on the backend; return a no-op client.
  if (LOCAL_MODE) {
    return {
      onAuthStateChange: () => () => {},
      signIn: () => {},
      signOut: async () => {},
    }
  }

  if (AUTH_PROVIDER === 'oidc') {
    return new OidcAuthClient({
      configUrl: import.meta.env.VITE_OIDC_CONFIG_URL as string,
      clientId: import.meta.env.VITE_OIDC_CLIENT_ID as string,
      redirectUri: window.location.origin,
      scope: import.meta.env.VITE_OIDC_SCOPE as string | undefined,
      bearer: (import.meta.env.VITE_OIDC_TOKEN as 'id' | 'access' | undefined) ?? 'id',
    })
  }

  // Default: Supabase — reuse the single shared client so session/storage match.
  const client = supabaseClient!

  return {
    onAuthStateChange(callback) {
      const { data: { subscription } } = client.auth.onAuthStateChange((_event, session) => {
        callback(session ? mapSupabaseSession(session) : null)
      })
      return () => subscription.unsubscribe()
    },
    signIn() {
      client.auth.signInWithOAuth({
        provider: 'google',
        options: { redirectTo: window.location.origin },
      })
    },
    async signOut() {
      await client.auth.signOut()
    },
  }
}

function mapSupabaseSession(session: { access_token: string; user: { id: string; email?: string; user_metadata?: Record<string, unknown> } }): AuthSession {
  return {
    access_token: session.access_token,
    user: {
      id: session.user.id,
      email: session.user.email,
      user_metadata: session.user.user_metadata as { full_name?: string } | undefined,
    },
  }
}

export const authClient: AuthClient = createAuthClient()
