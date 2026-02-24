import { useState, useEffect } from 'react'
import MarkdownViewer from './components/MarkdownViewer'
import MarkdownEditor from './components/MarkdownEditor'
import ChatPanel from './components/ChatPanel'

// In dev mode always use the Vite proxy (/api → localhost:8000) regardless of
// any VITE_API_BASE shell variable that may be set from running the deploy script.
// In production the build sets VITE_API_BASE to the ALB URL at build time.
const API_BASE = import.meta.env.DEV ? '/api' : (import.meta.env.VITE_API_BASE ?? '/api')

// Derive the site name from the first URL path segment so the frontend always
// knows which S3 prefix it is operating under.
// Production: /knuth-home/  →  "knuth-home"
// Dev with no path segment  →  VITE_SITE env var, or "default"
function getSite(): string {
  const first = window.location.pathname.split('/').filter(Boolean)[0]
  return first ?? import.meta.env.VITE_SITE ?? 'default'
}

const SITE = getSite()

// Derive the file path from the URL hash.
// #/agents/myagent  →  agents/myagent/agents.md
// (anything else)   →  content.md
function getFilePath(): string {
  const hash = window.location.hash
  const match = hash.match(/^#\/agents\/([a-z0-9][a-z0-9_-]*)$/)
  if (match) return `agents/${match[1]}/agents.md`
  return 'content.md'
}

// Extract agent name from file path for display purposes.
function getAgentName(filePath: string): string | null {
  const match = filePath.match(/^agents\/([a-z0-9][a-z0-9_-]*)\/agents\.md$/)
  return match ? match[1] : null
}

type Mode = 'view' | 'edit'

export default function App() {
  const [filePath, setFilePath] = useState(getFilePath)
  const [content, setContent] = useState<string | null>(null)
  const [savedContent, setSavedContent] = useState<string>('')
  const [mode, setMode] = useState<Mode>('view')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  // Update filePath on hash navigation
  useEffect(() => {
    const handler = () => setFilePath(getFilePath())
    window.addEventListener('hashchange', handler)
    return () => window.removeEventListener('hashchange', handler)
  }, [])

  // Fetch the markdown file (relative URL resolves against S3 origin)
  useEffect(() => {
    setContent(null)
    setError(null)
    fetch(filePath)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load content: ${res.status}`)
        return res.text()
      })
      .then((text) => {
        setContent(text)
        setSavedContent(text)
      })
      .catch((err) => setError(err.message))
  }, [filePath])

  async function save() {
    if (content === null) return
    setSaving(true)
    try {
      const res = await fetch(
        `${API_BASE}/content?site=${encodeURIComponent(SITE)}&path=${encodeURIComponent(filePath)}`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'text/plain' },
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
    setMode('view')
  }

  const isDirty = content !== savedContent
  const agentName = getAgentName(filePath)

  return (
    <>
      <header className="topbar">
        <span className="topbar-title">
          AgentScribe
          {agentName && <span className="topbar-subtitle"> — Agent: {agentName}</span>}
        </span>
        <div className="topbar-actions">
          {mode === 'view' ? (
            <button className="btn" onClick={() => setMode('edit')}>
              Edit
            </button>
          ) : (
            <>
              <button className="btn btn-danger" onClick={discard}>
                Discard
              </button>
              <button
                className="btn btn-primary"
                onClick={save}
                disabled={!isDirty || saving}
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
              <button className="btn" onClick={() => setMode('view')}>
                Preview
              </button>
            </>
          )}
        </div>
      </header>

      <div className="workspace">
        {mode === 'edit' && content !== null && (
          <ChatPanel
            content={content}
            onContentUpdate={setContent}
            apiBase={API_BASE}
            site={SITE}
            filePath={filePath}
          />
        )}

        <div className="content-area">
          {error ? (
            <div className="state-center">{error}</div>
          ) : content === null ? (
            <div className="state-center">Loading…</div>
          ) : mode === 'view' ? (
            <MarkdownViewer content={content} />
          ) : (
            <MarkdownEditor content={content} onChange={setContent} />
          )}
        </div>
      </div>
    </>
  )
}
