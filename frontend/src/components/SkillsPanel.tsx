import { useState, useEffect } from 'react'
import MarkdownEditor from './MarkdownEditor'
import ChatPanel from './ChatPanel'

interface Props {
  apiBase: string
  site: string
  token: string
}

export default function SkillsPanel({ apiBase, site, token }: Props) {
  const [skills, setSkills] = useState<string[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [content, setContent] = useState<string>('')
  const [savedContent, setSavedContent] = useState<string>('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    fetch(`${apiBase}/skills?site=${encodeURIComponent(site)}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((res) => (res.ok ? res.json() : { skills: [] }))
      .then((data) => setSkills(data.skills ?? []))
      .catch(() => {})
  }, [apiBase, site, token])

  function selectSkill(name: string) {
    setSelected(name)
    setLoading(true)
    const path = `.skills/${name}/SKILL.md`
    fetch(`${apiBase}/content?site=${encodeURIComponent(site)}&path=${encodeURIComponent(path)}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((res) => (res.ok ? res.text() : ''))
      .then((text) => {
        setContent(text)
        setSavedContent(text)
      })
      .catch(() => {
        setContent('')
        setSavedContent('')
      })
      .finally(() => setLoading(false))
  }

  async function save() {
    if (!selected) return
    setSaving(true)
    const path = `.skills/${selected}/SKILL.md`
    try {
      const res = await fetch(
        `${apiBase}/content?site=${encodeURIComponent(site)}&path=${encodeURIComponent(path)}`,
        {
          method: 'PUT',
          headers: {
            'Content-Type': 'text/plain',
            Authorization: `Bearer ${token}`,
          },
          body: content,
        }
      )
      if (!res.ok) throw new Error(`Save failed: ${res.status}`)
      setSavedContent(content)
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  function discard() {
    setContent(savedContent)
  }

  const isDirty = content !== savedContent
  const filePath = selected ? `.skills/${selected}/SKILL.md` : 'content.md'

  return (
    <>
      <div className="agents-panel">
        <div className="agents-panel-header">Skills</div>
        <div className="agents-list">
          {skills.length === 0 ? (
            <div className="agents-empty">No skills yet. Create one via chat.</div>
          ) : (
            skills.map((name) => (
              <button
                key={name}
                className={`agents-item${selected === name ? ' active' : ''}`}
                onClick={() => selectSkill(name)}
              >
                {name}
              </button>
            ))
          )}
        </div>
      </div>
      <ChatPanel
        content={content}
        onContentUpdate={setContent}
        apiBase={apiBase}
        site={site}
        filePath={filePath}
        token={token}
      />
      <div className="content-area">
        {selected === null ? (
          <div className="state-center">Select a skill to edit, or ask the assistant to create one.</div>
        ) : loading ? (
          <div className="state-center">Loading…</div>
        ) : (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', padding: '0.5rem 1rem', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
              <span style={{ fontWeight: 600 }}>{selected}</span>
              <span style={{ flex: 1 }} />
              <button className="btn btn-danger" onClick={discard} disabled={!isDirty}>Discard</button>
              <button className="btn btn-primary" onClick={save} disabled={!isDirty || saving}>
                {saving ? 'Saving…' : 'Save'}
              </button>
            </div>
            <MarkdownEditor key={selected} content={content} onChange={setContent} />
          </>
        )}
      </div>
    </>
  )
}
