import { useState, useEffect } from 'react'

interface SharedUser {
  email: string
  access: 'view' | 'write'
}

interface Settings {
  visibility: 'public' | 'private' | 'shared'
  shared_with: SharedUser[]
}

interface Props {
  apiBase: string
  site: string
  filePath: string
  token: string
  onClose: () => void
}

export default function PageSettingsPanel({ apiBase, site, filePath, token, onClose }: Props) {
  const [settings, setSettings] = useState<Settings>({ visibility: 'private', shared_with: [] })
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [newEmail, setNewEmail] = useState('')
  const [newAccess, setNewAccess] = useState<'view' | 'write'>('view')

  useEffect(() => {
    setLoading(true)
    setError(null)
    fetch(`${apiBase}/settings?site=${encodeURIComponent(site)}&path=${encodeURIComponent(filePath)}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`Failed to load settings: ${res.status}`))))
      .then((data: Settings) => setSettings(data))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [apiBase, site, filePath, token])

  async function save() {
    setSaving(true)
    setError(null)
    try {
      const res = await fetch(
        `${apiBase}/settings?site=${encodeURIComponent(site)}&path=${encodeURIComponent(filePath)}`,
        {
          method: 'PUT',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify(settings),
        },
      )
      if (!res.ok) throw new Error(`Save failed: ${res.status}`)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  function addUser() {
    const email = newEmail.trim().toLowerCase()
    if (!email) return
    if (settings.shared_with.some((u) => u.email === email)) return
    setSettings((s) => ({ ...s, shared_with: [...s.shared_with, { email, access: newAccess }] }))
    setNewEmail('')
    setNewAccess('view')
  }

  function removeUser(email: string) {
    setSettings((s) => ({ ...s, shared_with: s.shared_with.filter((u) => u.email !== email) }))
  }

  function setUserAccess(email: string, access: 'view' | 'write') {
    setSettings((s) => ({
      ...s,
      shared_with: s.shared_with.map((u) => (u.email === email ? { ...u, access } : u)),
    }))
  }

  if (loading) return <div className="state-center">Loading settings…</div>

  return (
    <div style={{ maxWidth: 520, margin: '2rem auto', padding: '0 1rem' }}>
      <h2 style={{ marginBottom: '1.25rem', fontSize: '1.1rem', fontWeight: 600 }}>Page Settings</h2>

      {error && (
        <div style={{ color: 'var(--danger, #e53e3e)', marginBottom: '1rem', fontSize: '0.875rem' }}>
          {error}
        </div>
      )}

      <fieldset style={{ border: 'none', marginBottom: '1.5rem', padding: 0 }}>
        <legend style={{ fontWeight: 600, marginBottom: '0.75rem', fontSize: '0.875rem' }}>Visibility</legend>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
          {(['public', 'private', 'shared'] as const).map((v) => (
            <label
              key={v}
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: '0.625rem',
                padding: '0.625rem 0.875rem',
                borderRadius: 8,
                border: `1px solid ${settings.visibility === v ? 'var(--success, #38a169)' : 'var(--border)'}`,
                background: settings.visibility === v ? 'var(--surface-raised)' : 'transparent',
                cursor: 'pointer',
              }}
            >
              <input
                type="radio"
                name="visibility"
                value={v}
                checked={settings.visibility === v}
                onChange={() => setSettings((s) => ({ ...s, visibility: v }))}
                style={{ marginTop: 2 }}
              />
              <div>
                <div style={{ fontWeight: 500, fontSize: '0.875rem', textTransform: 'capitalize' }}>{v}</div>
                <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginTop: 2 }}>
                  {v === 'public' && 'Anyone with the URL can view this page. No login required.'}
                  {v === 'private' && 'Only you can view and edit this page.'}
                  {v === 'shared' && 'Visible to specific users you invite by email.'}
                </div>
              </div>
            </label>
          ))}
        </div>
      </fieldset>

      {settings.visibility === 'shared' && (
        <div style={{ marginBottom: '1.5rem' }}>
          <div style={{ fontWeight: 600, marginBottom: '0.75rem', fontSize: '0.875rem' }}>Shared With</div>

          {settings.shared_with.length === 0 ? (
            <div style={{ fontSize: '0.875rem', color: 'var(--text-muted)', marginBottom: '0.75rem' }}>
              No users added yet.
            </div>
          ) : (
            <table style={{ width: '100%', borderCollapse: 'collapse', marginBottom: '0.75rem' }}>
              <thead>
                <tr style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                  <th style={{ textAlign: 'left', padding: '0.25rem 0.5rem', fontWeight: 500 }}>Email</th>
                  <th style={{ textAlign: 'left', padding: '0.25rem 0.5rem', fontWeight: 500 }}>Access</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {settings.shared_with.map((u) => (
                  <tr key={u.email} style={{ borderTop: '1px solid var(--border)' }}>
                    <td style={{ padding: '0.4rem 0.5rem', fontSize: '0.875rem' }}>{u.email}</td>
                    <td style={{ padding: '0.4rem 0.5rem' }}>
                      <select
                        value={u.access}
                        onChange={(e) => setUserAccess(u.email, e.target.value as 'view' | 'write')}
                        style={{
                          background: 'var(--surface-raised)',
                          border: '1px solid var(--border)',
                          borderRadius: 4,
                          color: 'var(--text)',
                          fontSize: '0.8rem',
                          padding: '0.15rem 0.4rem',
                        }}
                      >
                        <option value="view">View</option>
                        <option value="write">Write</option>
                      </select>
                    </td>
                    <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right' }}>
                      <button
                        className="btn btn-danger"
                        style={{ padding: '0.2rem 0.5rem', fontSize: '0.75rem' }}
                        onClick={() => removeUser(u.email)}
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
            <input
              type="email"
              placeholder="Email address"
              value={newEmail}
              onChange={(e) => setNewEmail(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addUser() } }}
              style={{
                flex: 1,
                padding: '0.35rem 0.6rem',
                borderRadius: 6,
                border: '1px solid var(--border)',
                background: 'var(--surface-raised)',
                color: 'var(--text)',
                fontSize: '0.875rem',
              }}
            />
            <select
              value={newAccess}
              onChange={(e) => setNewAccess(e.target.value as 'view' | 'write')}
              style={{
                background: 'var(--surface-raised)',
                border: '1px solid var(--border)',
                borderRadius: 6,
                color: 'var(--text)',
                fontSize: '0.875rem',
                padding: '0.35rem 0.5rem',
              }}
            >
              <option value="view">View</option>
              <option value="write">Write</option>
            </select>
            <button className="btn" onClick={addUser}>Add</button>
          </div>
        </div>
      )}

      <div style={{ display: 'flex', gap: '0.5rem', justifyContent: 'flex-end' }}>
        <button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  )
}
