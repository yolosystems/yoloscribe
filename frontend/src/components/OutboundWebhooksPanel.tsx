import { useState, useEffect } from 'react'
import { Trash2 } from 'lucide-react'

interface WebhookEntry {
  index: number
  label: string
  url: string
}

interface Props {
  apiBase: string
  token: string
}

export default function OutboundWebhooksPanel({ apiBase, token }: Props) {
  const [webhooks, setWebhooks] = useState<WebhookEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [newLabel, setNewLabel] = useState('')
  const [newUrl, setNewUrl] = useState('')
  const [adding, setAdding] = useState(false)
  const [deleting, setDeleting] = useState<Record<number, boolean>>({})

  useEffect(() => { load() }, [apiBase])

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${apiBase}/outbound-webhooks`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setWebhooks(data.webhooks)
    } catch (e) {
      setError(`Failed to load webhooks: ${e instanceof Error ? e.message : e}`)
    } finally {
      setLoading(false)
    }
  }

  async function addWebhook() {
    const url = newUrl.trim()
    if (!url) return
    setAdding(true)
    try {
      const res = await fetch(`${apiBase}/outbound-webhooks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({ label: newLabel.trim(), url }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail ?? `HTTP ${res.status}`)
      }
      setNewLabel('')
      setNewUrl('')
      await load()
    } catch (e) {
      alert(`Failed to add webhook: ${e instanceof Error ? e.message : e}`)
    } finally {
      setAdding(false)
    }
  }

  async function deleteWebhook(index: number) {
    setDeleting((prev) => ({ ...prev, [index]: true }))
    try {
      const res = await fetch(`${apiBase}/outbound-webhooks/${index}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await load()
    } catch (e) {
      alert(`Failed to delete webhook: ${e instanceof Error ? e.message : e}`)
    } finally {
      setDeleting((prev) => ({ ...prev, [index]: false }))
    }
  }

  return (
    <div className="credentials-section">
      <div className="credentials-section-header">Webhooks</div>
      <p className="credentials-section-description">
        Agent notifications are delivered to these URLs. Any webhook works — Discord, Slack, Teams, or custom endpoints.
      </p>

      {error && <div className="credentials-banner credentials-banner--error">{error}</div>}

      {loading ? (
        <div className="credentials-empty">Loading…</div>
      ) : (
        <>
          {webhooks.length === 0 ? (
            <p className="credentials-empty">No webhooks configured.</p>
          ) : (
            <div className="credentials-webhook-list">
              {webhooks.map((w) => (
                <div key={w.index} className="credentials-webhook-row">
                  <div className="credentials-webhook-info">
                    {w.label && <span className="credentials-webhook-label">{w.label}</span>}
                    <code className="credentials-webhook-url">{w.url}</code>
                  </div>
                  <button
                    className="btn btn-danger btn-icon"
                    onClick={() => deleteWebhook(w.index)}
                    disabled={deleting[w.index]}
                    title="Delete webhook"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}

          <div className="credentials-webhook-add">
            <input
              type="text"
              className="credentials-input"
              placeholder="Label (optional)"
              value={newLabel}
              onChange={(e) => setNewLabel(e.target.value)}
            />
            <input
              type="url"
              className="credentials-input"
              placeholder="https://discord.com/api/webhooks/…"
              value={newUrl}
              onChange={(e) => setNewUrl(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && addWebhook()}
            />
            <button
              className="btn btn-primary"
              onClick={addWebhook}
              disabled={adding || !newUrl.trim()}
            >
              {adding ? 'Adding…' : 'Add Webhook'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}
