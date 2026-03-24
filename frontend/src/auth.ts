/**
 * Provider-agnostic auth client.
 *
 * Selected at build time via VITE_AUTH_PROVIDER=supabase|cognito (default: supabase).
 * Both implementations expose the same AuthClient interface so App.tsx has no
 * provider-specific logic.
 */

import { createClient } from '@supabase/supabase-js'
import { CognitoAuthClient } from './auth_cognito'

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

function createAuthClient(): AuthClient {
  // In LOCAL_MODE auth is bypassed entirely on the backend; return a no-op client.
  if (LOCAL_MODE) {
    return {
      onAuthStateChange: () => () => {},
      signIn: () => {},
      signOut: async () => {},
    }
  }

  if (AUTH_PROVIDER === 'cognito') {
    return new CognitoAuthClient({
      clientId: import.meta.env.VITE_COGNITO_CLIENT_ID as string,
      domain: import.meta.env.VITE_COGNITO_DOMAIN as string,
      redirectUri: window.location.origin,
    })
  }

  // Default: Supabase
  const client = createClient(
    import.meta.env.VITE_SUPABASE_URL as string,
    import.meta.env.VITE_SUPABASE_ANON_KEY as string,
    { auth: { flowType: 'implicit' } },
  )

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
