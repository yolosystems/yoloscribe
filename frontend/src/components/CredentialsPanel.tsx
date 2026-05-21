import { useState, useEffect } from 'react'
import OutboundWebhooksPanel from './OutboundWebhooksPanel'

// ── API response types ────────────────────────────────────────────────────────

interface OAuthSkill {
  type: 'oauth'
  authenticated: boolean
  expires_at: string | null
  scope: string | null
}

interface KeySkill {
  type: 'key'
  vars: string[]
  stored: Record<string, boolean>
}

type SkillStatus = OAuthSkill | KeySkill

interface SecretsStatus {
  skills: Record<string, SkillStatus>
}

interface Props {
  apiBase: string
  token: string
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function CredentialsPanel({ apiBase, token }: Props) {
  const [status, setStatus] = useState<SecretsStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Key-based skill state
  const [inputs, setInputs] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState<Record<string, boolean>>({})
  const [flashSaved, setFlashSaved] = useState<Record<string, boolean>>({})
  // OAuth flow state
  const [initiating, setInitiating] = useState<Record<string, boolean>>({})
  const [oauthBanner, setOAuthBanner] = useState<{ skill: string; kind: 'success' | 'error'; message: string } | null>(null)

  // On mount: check if we're returning from an OAuth redirect
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const successSkill = params.get('oauth_success')
    const errorMsg = params.get('oauth_error')

    if (successSkill || errorMsg) {
      // Clean up the URL so the params don't linger
      params.delete('oauth_success')
      params.delete('oauth_error')
      const newSearch = params.toString()
      window.history.replaceState({}, '', window.location.pathname + (newSearch ? '?' + newSearch : ''))

      if (errorMsg) {
        setOAuthBanner({ skill: '', kind: 'error', message: decodeURIComponent(errorMsg) })
      } else if (successSkill) {
        setOAuthBanner({ skill: successSkill, kind: 'success', message: `Successfully authenticated ${successSkill}.` })
      }
    }

    loadStatus()
  }, [apiBase])

  function loadStatus() {
    fetch(`${apiBase}/secrets/status`, {
      headers: { ...(token && { Authorization: `Bearer ${token}` }) },
    })
      .then((res) => (res.ok ? res.json() : Promise.reject(`HTTP ${res.status}`)))
      .then((data: SecretsStatus) => setStatus(data))
      .catch((e) => setError(`Failed to load credentials: ${e}`))
  }

  // ── OAuth flow ──────────────────────────────────────────────────────────────

  async function startOAuth(skillName: string) {
    setInitiating((prev) => ({ ...prev, [skillName]: true }))
    try {
      const res = await fetch(`${apiBase}/oauth/initiate/${encodeURIComponent(skillName)}`, {
        method: 'POST',
        headers: { ...(token && { Authorization: `Bearer ${token}` }) },
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail ?? `HTTP ${res.status}`)
      }
      const { auth_url } = await res.json()
      window.location.href = auth_url
    } catch (e) {
      setInitiating((prev) => ({ ...prev, [skillName]: false }))
      setOAuthBanner({ skill: skillName, kind: 'error', message: `Failed to start OAuth for ${skillName}: ${e}` })
    }
  }

  // ── Key-based credential save ───────────────────────────────────────────────

  async function saveVar(varName: string) {
    const value = inputs[varName]?.trim()
    if (!value) return
    setSaving((prev) => ({ ...prev, [varName]: true }))
    try {
      const res = await fetch(`${apiBase}/secrets/${encodeURIComponent(varName)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...(token && { Authorization: `Bearer ${token}` }) },
        body: JSON.stringify({ value }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setStatus((prev) => {
        if (!prev) return prev
        const next: SecretsStatus = { skills: {} }
        for (const [skill, data] of Object.entries(prev.skills)) {
          if (data.type === 'key' && varName in data.stored) {
            next.skills[skill] = { ...data, stored: { ...data.stored, [varName]: true } }
          } else {
            next.skills[skill] = data
          }
        }
        return next
      })
      setInputs((prev) => ({ ...prev, [varName]: '' }))
      setFlashSaved((prev) => ({ ...prev, [varName]: true }))
      setTimeout(() => setFlashSaved((prev) => ({ ...prev, [varName]: false })), 2000)
    } catch (e) {
      alert(`Failed to save ${varName}: ${e instanceof Error ? e.message : e}`)
    } finally {
      setSaving((prev) => ({ ...prev, [varName]: false }))
    }
  }

  // ── Render ──────────────────────────────────────────────────────────────────

  if (error) return <div className="state-center">{error}</div>
  if (!status) return <div className="state-center">Loading…</div>

  const skills = Object.entries(status.skills)

  return (
    <div className="credentials-panel">
      <div className="credentials-header">Credentials</div>
      <div className="credentials-body">
        {oauthBanner && (
          <div className={`credentials-banner credentials-banner--${oauthBanner.kind}`}>
            {oauthBanner.message}
            <button className="credentials-banner-dismiss" onClick={() => setOAuthBanner(null)}>✕</button>
          </div>
        )}
        {skills.length === 0 && (
          <p className="credentials-empty">No skills configured on this server.</p>
        )}
        {skills.map(([skillName, skill]) => (
          <div key={skillName} className="credentials-skill">
            <div className="credentials-skill-name">{skillName}</div>

            {skill.type === 'oauth' ? (
              // ── OAuth skill ───────────────────────────────────────────────
              <div className="credentials-oauth">
                {skill.authenticated ? (
                  <div className="credentials-oauth-status">
                    <span className="credentials-badge credentials-badge--stored">Authenticated ✓</span>
                    {skill.expires_at && (
                      <span className="credentials-oauth-expiry">
                        expires {new Date(skill.expires_at).toLocaleDateString()}
                      </span>
                    )}
                    {skill.scope && (
                      <span className="credentials-oauth-scope">scope: {skill.scope}</span>
                    )}
                  </div>
                ) : (
                  <span className="credentials-badge credentials-badge--missing">Not authenticated</span>
                )}
                <button
                  className="btn btn-primary"
                  onClick={() => startOAuth(skillName)}
                  disabled={initiating[skillName]}
                >
                  {initiating[skillName]
                    ? 'Redirecting…'
                    : skill.authenticated
                    ? 'Re-authenticate'
                    : 'Authenticate via OAuth'}
                </button>
              </div>
            ) : (
              // ── Key-based skill ───────────────────────────────────────────
              skill.vars.length === 0 ? (
                <p className="credentials-no-vars">No credentials required.</p>
              ) : (
                skill.vars.map((varName) => (
                  <div key={varName} className="credentials-var">
                    <div className="credentials-var-label">
                      <code className="credentials-var-name">{varName}</code>
                      {skill.stored[varName] ? (
                        <span className="credentials-badge credentials-badge--stored">Stored ✓</span>
                      ) : (
                        <span className="credentials-badge credentials-badge--missing">Not stored</span>
                      )}
                    </div>
                    <div className="credentials-var-row">
                      <input
                        type="password"
                        className="credentials-input"
                        placeholder={skill.stored[varName] ? 'Enter new value to update…' : 'Enter value…'}
                        value={inputs[varName] ?? ''}
                        onChange={(e) =>
                          setInputs((prev) => ({ ...prev, [varName]: e.target.value }))
                        }
                        onKeyDown={(e) => e.key === 'Enter' && saveVar(varName)}
                      />
                      <button
                        className="btn btn-primary"
                        onClick={() => saveVar(varName)}
                        disabled={saving[varName] || !inputs[varName]?.trim()}
                      >
                        {flashSaved[varName] ? 'Saved!' : saving[varName] ? 'Saving…' : 'Save'}
                      </button>
                    </div>
                  </div>
                ))
              )
            )}
          </div>
        ))}
        <OutboundWebhooksPanel apiBase={apiBase} token={token} />
      </div>
    </div>
  )
}
