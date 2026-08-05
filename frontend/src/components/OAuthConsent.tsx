/**
 * OAuth 2.1 consent screen for YoloScribe's MCP server (YOL-505 inbound auth).
 *
 * Supabase's OAuth 2.1 server is headless — after `/authorize` it redirects the
 * browser here (`/oauth/consent?authorization_id=...`) to render the approval UI.
 * We fetch the request details, show a YoloScribe-branded grant screen, and call
 * Supabase's approve/deny API — which mints the code and redirects back to the
 * OAuth client (LiteLLM). All OAuth protocol logic stays inside Supabase; this is
 * purely the branded consent surface.
 */
import { useEffect, useState } from 'react'
import { supabaseClient } from '../auth'
import YoloScribeMark from './YoloScribeMark'

type OAuthClient = { name?: string; client_name?: string; client_id?: string; logo_uri?: string }
type Details = {
  authorization_id: string
  redirect_uri: string
  client: OAuthClient
  user: { id: string; email: string }
  scope: string
}
type Phase = 'loading' | 'login' | 'consent' | 'submitting' | 'error'

// yolo palette, inlined so the page is on-brand regardless of the active theme.
const C = {
  bg: '#05050f', surface: '#0c0c1e', raised: '#14143a', border: '#2a1f6e',
  text: '#f5f0ff', muted: '#8878bb', accent: '#ff4500', accentHover: '#ff6f3c',
  danger: '#ff4569',
}

const SCOPE_LABELS: Record<string, string> = {
  openid: 'Verify your identity',
  profile: 'Read your basic profile',
  email: 'Read your email address',
  offline_access: 'Stay connected when you’re away',
}

export default function OAuthConsent() {
  const [phase, setPhase] = useState<Phase>('loading')
  const [details, setDetails] = useState<Details | null>(null)
  const [error, setError] = useState('')

  const authorizationId = new URLSearchParams(window.location.search).get('authorization_id') || ''

  useEffect(() => {
    void (async () => {
      if (!supabaseClient) {
        setError('This YoloScribe deployment is not configured for Supabase OAuth.')
        setPhase('error'); return
      }
      if (!authorizationId) {
        setError('Missing authorization_id — this page should be reached from an authorization request.')
        setPhase('error'); return
      }
      const { data: { session } } = await supabaseClient.auth.getSession()
      if (!session) { setPhase('login'); return }

      const { data, error: err } = await supabaseClient.auth.oauth.getAuthorizationDetails(authorizationId)
      if (err) { setError(err.message); setPhase('error'); return }
      if (data && 'redirect_url' in data) { window.location.href = data.redirect_url; return } // already consented
      setDetails(data as Details)
      setPhase('consent')
    })()
  }, [authorizationId])

  function signIn() {
    // Return to this exact consent URL (authorization_id preserved) after login.
    supabaseClient?.auth.signInWithOAuth({ provider: 'google', options: { redirectTo: window.location.href } })
  }

  async function decide(approve: boolean) {
    if (!supabaseClient) return
    setPhase('submitting')
    const oauth = supabaseClient.auth.oauth
    const { data, error: err } = approve
      ? await oauth.approveAuthorization(authorizationId, { skipBrowserRedirect: true })
      : await oauth.denyAuthorization(authorizationId, { skipBrowserRedirect: true })
    if (err) { setError(err.message); setPhase('error'); return }
    if (data && 'redirect_url' in data) window.location.href = data.redirect_url
  }

  const clientName = details?.client?.name || details?.client?.client_name || 'An application'
  const scopes = (details?.scope || '').split(/\s+/).filter(Boolean)

  return (
    <div style={S.page}>
      <div style={S.card}>
        <div style={S.brandRow}>
          <YoloScribeMark size={56} />
          <span style={S.wordmark}>YoloScribe</span>
        </div>

        {phase === 'loading' && <p style={S.status}>Loading authorization request…</p>}

        {phase === 'submitting' && <p style={S.status}>Completing…</p>}

        {phase === 'error' && (
          <>
            <h1 style={S.title}>Something went wrong</h1>
            <p style={{ ...S.sub, color: C.danger }}>{error}</p>
          </>
        )}

        {phase === 'login' && (
          <>
            <h1 style={S.title}>Sign in to continue</h1>
            <p style={S.sub}>You need to sign in to your YoloScribe account before granting access.</p>
            <button style={S.primary} onClick={signIn}>Sign in with Google</button>
          </>
        )}

        {phase === 'consent' && details && (
          <>
            <h1 style={S.title}>Authorize access</h1>
            <p style={S.sub}>
              <strong style={{ color: C.text }}>{clientName}</strong> wants to connect to your YoloScribe account.
            </p>

            <div style={S.scopeBox}>
              <div style={S.scopeHeader}>This will allow it to:</div>
              <ul style={S.scopeList}>
                {scopes.map((s) => (
                  <li key={s} style={S.scopeItem}>
                    <span style={S.scopeDot} />
                    <span>{SCOPE_LABELS[s] || s}</span>
                  </li>
                ))}
                <li style={S.scopeItem}>
                  <span style={S.scopeDot} />
                  <span>Read and write your wiki through the YoloScribe MCP</span>
                </li>
              </ul>
            </div>

            <p style={S.signedIn}>Signed in as {details.user.email}</p>

            <div style={S.actions}>
              <button style={S.secondary} onClick={() => decide(false)}>Deny</button>
              <button style={S.primary} onClick={() => decide(true)}>Authorize</button>
            </div>

            <p style={S.redirectNote}>You’ll be returned to {safeHost(details.redirect_uri)}</p>
          </>
        )}
      </div>
    </div>
  )
}

function safeHost(uri: string): string {
  try { return new URL(uri).host } catch { return 'the application' }
}

const S: Record<string, React.CSSProperties> = {
  page: {
    minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center',
    background: `radial-gradient(1200px 600px at 50% -10%, ${C.raised}, ${C.bg})`,
    padding: 24, boxSizing: 'border-box',
    fontFamily: 'ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif',
  },
  card: {
    width: '100%', maxWidth: 420, background: C.surface, border: `1px solid ${C.border}`,
    borderRadius: 18, padding: '32px 28px', boxShadow: '0 24px 60px -20px rgba(0,0,0,0.7)',
    color: C.text,
  },
  brandRow: { display: 'flex', alignItems: 'center', gap: 12, marginBottom: 24 },
  wordmark: { fontSize: 20, fontWeight: 700, letterSpacing: '-0.01em' },
  title: { fontSize: 22, fontWeight: 700, margin: '0 0 8px' },
  sub: { fontSize: 15, lineHeight: 1.5, color: C.muted, margin: '0 0 20px' },
  status: { fontSize: 15, color: C.muted, margin: '8px 0' },
  scopeBox: { background: C.raised, border: `1px solid ${C.border}`, borderRadius: 12, padding: '14px 16px', marginBottom: 18 },
  scopeHeader: { fontSize: 13, color: C.muted, marginBottom: 10, textTransform: 'uppercase', letterSpacing: '0.04em' },
  scopeList: { listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 10 },
  scopeItem: { display: 'flex', alignItems: 'center', gap: 10, fontSize: 14.5 },
  scopeDot: { width: 6, height: 6, borderRadius: '50%', background: C.accent, flex: '0 0 auto' },
  signedIn: { fontSize: 13, color: C.muted, margin: '0 0 20px' },
  actions: { display: 'flex', gap: 12 },
  primary: {
    flex: 1, padding: '12px 16px', borderRadius: 10, border: 'none', cursor: 'pointer',
    background: C.accent, color: '#fff', fontSize: 15, fontWeight: 600,
  },
  secondary: {
    flex: 1, padding: '12px 16px', borderRadius: 10, cursor: 'pointer',
    background: 'transparent', color: C.text, border: `1px solid ${C.border}`, fontSize: 15, fontWeight: 600,
  },
  redirectNote: { fontSize: 12, color: C.muted, textAlign: 'center', margin: '16px 0 0' },
}
