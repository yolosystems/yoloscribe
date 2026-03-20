import { useState, FormEvent } from 'react'

interface Props {
  apiBase: string
  site: string
  token: string
  pagePath: string
  onSuccess: (agentName: string) => void
  onClose: () => void
}

const AGENT_NAME_RE = /^[a-z0-9][a-z0-9_-]*$/

export default function CreateAgentModal({ apiBase, site, token, pagePath, onSuccess, onClose }: Props) {
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const isValid = AGENT_NAME_RE.test(name)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!isValid) return
    setSubmitting(true)
    setError(null)
    try {
      const res = await fetch(`${apiBase}/agents`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ site, page_path: pagePath, agent_name: name }),
      })
      if (res.status === 409) {
        setError('An agent with this name already exists.')
        return
      }
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        setError(data.detail ?? `Error ${res.status}`)
        return
      }
      onSuccess(name)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <div className="modal-overlay" onClick={onClose} />
      <div className="modal-dialog">
        <div className="modal-title">Create New Agent</div>
        <form className="modal-body" onSubmit={handleSubmit}>
          <div className="form-field">
            <label htmlFor="agent-name">Agent name</label>
            <input
              id="agent-name"
              className="form-input"
              type="text"
              autoFocus
              placeholder="e.g. summarizer"
              value={name}
              onChange={(e) => { setName(e.target.value.toLowerCase()); setError(null) }}
            />
            <span className="form-hint">
              Lowercase letters, digits, hyphens, and underscores only.
              {name && !isValid && <span style={{ color: 'var(--danger-text)' }}> Invalid name.</span>}
            </span>
          </div>
          {error && <div className="form-error">{error}</div>}
          <div className="modal-actions">
            <button type="button" className="btn" onClick={onClose} disabled={submitting}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={!isValid || submitting}>
              {submitting ? 'Creating…' : 'Create Agent'}
            </button>
          </div>
        </form>
      </div>
    </>
  )
}
