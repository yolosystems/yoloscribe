import { useState, useEffect, useRef } from 'react'

interface ApiToken {
  id: string
  name: string
  site_name: string
  created_at: string
  expires_at: string | null
  last_used_at: string | null
}

interface Props {
  apiBase: string
  token: string
}

function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

// ── One-time token reveal modal ────────────────────────────────────────────────

interface RevealModalProps {
  rawToken: string
  onClose: () => void
}

function RevealModal({ rawToken, onClose }: RevealModalProps) {
  const [copied, setCopied] = useState(false)

  function copy() {
    navigator.clipboard.writeText(rawToken).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  return (
    <>
      <div className="modal-overlay" onClick={onClose} />
      <div className="modal-dialog">
        <div className="modal-title">API Token Created</div>
        <div className="modal-body">
          <p>
            Copy your token now — it <strong>will not be shown again</strong>.
          </p>
          <div className="tokens-reveal-box">
            <code className="tokens-reveal-token">{rawToken}</code>
            <button className="btn btn-primary tokens-reveal-copy" onClick={copy}>
              {copied ? 'Copied!' : 'Copy'}
            </button>
          </div>
          <p className="tokens-rate-note">
            Rate limit: 10 requests / min · 100 requests / hr (on <code>/chat</code>)
          </p>
        </div>
        <div className="modal-actions">
          <button className="btn btn-primary" onClick={onClose}>Done</button>
        </div>
      </div>
    </>
  )
}

// ── TokensPanel ────────────────────────────────────────────────────────────────

export default function TokensPanel({ apiBase, token }: Props) {
  const [tokens, setTokens] = useState<ApiToken[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Creation form
  const [name, setName] = useState('')
  const [expiresAt, setExpiresAt] = useState('')
  const [creating, setCreating] = useState(false)

  // One-time reveal
  const [revealToken, setRevealToken] = useState<string | null>(null)

  // Revoke state
  const [revoking, setRevoking] = useState<Record<string, boolean>>({})

  const nameRef = useRef<HTMLInputElement>(null)

  useEffect(() => { load() }, [apiBase])

  function load() {
    setLoading(true)
    fetch(`${apiBase}/tokens`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((res) => (res.ok ? res.json() : Promise.reject(`HTTP ${res.status}`)))
      .then((data: ApiToken[]) => { setTokens(data); setLoading(false) })
      .catch((e) => { setError(`Failed to load tokens: ${e}`); setLoading(false) })
  }

  async function create() {
    const trimmed = name.trim()
    if (!trimmed) return
    setCreating(true)
    try {
      const body: Record<string, string> = { name: trimmed }
      if (expiresAt) body.expires_at = new Date(expiresAt).toISOString()
      const res = await fetch(`${apiBase}/tokens`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const j = await res.json().catch(() => ({}))
        throw new Error(j.detail ?? `HTTP ${res.status}`)
      }
      const created = await res.json()
      setRevealToken(created.token)
      setName('')
      setExpiresAt('')
      load()
    } catch (e) {
      alert(`Failed to create token: ${e instanceof Error ? e.message : e}`)
    } finally {
      setCreating(false)
    }
  }

  async function revoke(id: string) {
    setRevoking((prev) => ({ ...prev, [id]: true }))
    try {
      const res = await fetch(`${apiBase}/tokens/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setTokens((prev) => prev.filter((t) => t.id !== id))
    } catch (e) {
      alert(`Failed to revoke token: ${e instanceof Error ? e.message : e}`)
    } finally {
      setRevoking((prev) => ({ ...prev, [id]: false }))
    }
  }

  return (
    <>
      {revealToken && (
        <RevealModal rawToken={revealToken} onClose={() => setRevealToken(null)} />
      )}

      <div className="credentials-panel">
        <div className="credentials-header">API Tokens</div>
        <div className="credentials-body">

          {/* ── Create form ── */}
          <div className="tokens-create-card">
            <div className="tokens-create-title">New token</div>
            <div className="tokens-create-fields">
              <input
                ref={nameRef}
                className="credentials-input"
                placeholder="Token name (e.g. Discord bot)"
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && create()}
              />
              <input
                type="date"
                className="credentials-input tokens-date-input"
                title="Expiry date (optional)"
                value={expiresAt}
                onChange={(e) => setExpiresAt(e.target.value)}
              />
              <button
                className="btn btn-primary"
                onClick={create}
                disabled={creating || !name.trim()}
              >
                {creating ? 'Creating…' : 'Create'}
              </button>
            </div>
          </div>

          {/* ── Token list ── */}
          {error ? (
            <div className="credentials-empty">{error}</div>
          ) : loading ? (
            <div className="credentials-empty">Loading…</div>
          ) : tokens.length === 0 ? (
            <div className="credentials-empty">No API tokens yet.</div>
          ) : (
            <div className="tokens-list">
              {tokens.map((t) => (
                <div key={t.id} className="tokens-row">
                  <div className="tokens-row-info">
                    <span className="tokens-row-name">{t.name}</span>
                    <span className="tokens-row-meta">
                      Created {fmtDate(t.created_at)}
                      {t.last_used_at && ` · Last used ${fmtDate(t.last_used_at)}`}
                      {t.expires_at && ` · Expires ${fmtDate(t.expires_at)}`}
                    </span>
                  </div>
                  <button
                    className="btn btn-danger tokens-revoke-btn"
                    onClick={() => revoke(t.id)}
                    disabled={revoking[t.id]}
                  >
                    {revoking[t.id] ? 'Revoking…' : 'Revoke'}
                  </button>
                </div>
              ))}
            </div>
          )}

          <p className="tokens-rate-note">
            Rate limit per token: 10 requests / min · 100 requests / hr
          </p>

        </div>
      </div>
    </>
  )
}
