import { useState } from 'react'
import ThemePicker from './ThemePicker'

interface Props {
  apiBase: string
  token: string
  defaultSiteName: string
  onSuccess: (siteName: string) => void
}

export default function OnboardingView({ apiBase, token, defaultSiteName, onSuccess }: Props) {
  const [siteName, setSiteName] = useState(
    defaultSiteName.toLowerCase().replace(/[^a-z0-9-]/g, '-').replace(/^-+|-+$/g, '').slice(0, 50) || 'my-site',
  )
  const [theme, setTheme] = useState('dark')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      const res = await fetch(`${apiBase}/provision`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ site_name: siteName, theme }),
      })
      if (res.status === 409) {
        const data = await res.json().catch(() => ({ detail: '' }))
        setError(
          data.detail === 'Site name already taken'
            ? 'That name is already taken, try another'
            : data.detail || 'Conflict',
        )
        return
      }
      if (!res.ok) {
        const data = await res.json().catch(() => ({ detail: 'Provisioning failed' }))
        setError(data.detail ?? 'Provisioning failed')
        return
      }
      onSuccess(siteName)
    } catch {
      setError('Network error, please try again')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <header className="topbar">
        <span className="topbar-title">Yolo Scribe</span>
      </header>
      <div className="onboarding-page">
        <div className="onboarding-content">
          <h1>Set up your site</h1>
          <p className="onboarding-subtitle">Choose a name and theme for your new wiki site.</p>
          <form onSubmit={handleSubmit} className="onboarding-form">
            <div className="form-field">
              <label htmlFor="site-name">Site name</label>
              <input
                id="site-name"
                className="form-input"
                type="text"
                value={siteName}
                onChange={(e) => setSiteName(e.target.value.toLowerCase())}
                placeholder="my-awesome-site"
                required
              />
              <span className="form-hint">Lowercase letters, numbers, and hyphens only (3–50 characters)</span>
            </div>
            <div className="form-field">
              <label>Theme</label>
              <ThemePicker value={theme} onChange={setTheme} />
            </div>
            {error && <p className="form-error">{error}</p>}
            <button className="btn btn-primary form-submit" type="submit" disabled={submitting}>
              {submitting ? 'Creating…' : 'Create Site'}
            </button>
          </form>
        </div>
      </div>
    </>
  )
}
