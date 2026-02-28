import { useState, useEffect } from 'react'

interface SkillStatus {
  vars: string[]
  stored: Record<string, boolean>
}

interface SecretsStatus {
  skills: Record<string, SkillStatus>
}

interface Props {
  apiBase: string
  token: string
}

export default function CredentialsPanel({ apiBase, token }: Props) {
  const [status, setStatus] = useState<SecretsStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [inputs, setInputs] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState<Record<string, boolean>>({})
  const [flashSaved, setFlashSaved] = useState<Record<string, boolean>>({})

  useEffect(() => {
    fetch(`${apiBase}/secrets/status`, {
      headers: { ...(token && { Authorization: `Bearer ${token}` }) },
    })
      .then((res) => (res.ok ? res.json() : Promise.reject(`HTTP ${res.status}`)))
      .then((data: SecretsStatus) => setStatus(data))
      .catch((e) => setError(`Failed to load credentials: ${e}`))
  }, [apiBase])

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
      // Mark as stored in local state
      setStatus((prev) => {
        if (!prev) return prev
        const next: SecretsStatus = { skills: {} }
        for (const [skill, data] of Object.entries(prev.skills)) {
          next.skills[skill] = {
            vars: data.vars,
            stored: { ...data.stored, ...(varName in data.stored ? { [varName]: true } : {}) },
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

  if (error) return <div className="state-center">{error}</div>
  if (!status) return <div className="state-center">Loading…</div>

  const skills = Object.entries(status.skills)

  return (
    <div className="credentials-panel">
      <div className="credentials-header">Credentials</div>
      <div className="credentials-body">
        {skills.length === 0 && (
          <p className="credentials-empty">No skills configured on this server.</p>
        )}
        {skills.map(([skillName, skill]) => (
          <div key={skillName} className="credentials-skill">
            <div className="credentials-skill-name">{skillName}</div>
            {skill.vars.length === 0 ? (
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
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
