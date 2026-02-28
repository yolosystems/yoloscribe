import { useState, useEffect } from 'react'
import { type Session } from '@supabase/supabase-js'
import { supabase } from './supabase'
import MarkdownViewer from './components/MarkdownViewer'
import MarkdownEditor from './components/MarkdownEditor'
import ChatPanel from './components/ChatPanel'
import AgentsList from './components/AgentsList'
import Breadcrumb, { type BreadcrumbSegment } from './components/Breadcrumb'
import CredentialsPanel from './components/CredentialsPanel'

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

// ── Hash ↔ filePath helpers ────────────────────────────────────────────────────
//
// Hash scheme:
//   (empty)                      →  content.md              (root page)
//   #/{page}                     →  {page}/content.md       (child page)
//   #/.agents/{name}             →  .agents/{name}/agent.md (root agent)
//   #/{page}/.agents/{name}      →  {page}/.agents/{name}/agent.md

function getFilePath(): string {
  const hash = window.location.hash
  if (!hash) return 'content.md'
  const path = hash.replace(/^#\/?/, '')
  if (!path) return 'content.md'
  // {page}/.agents/{name}  or  .agents/{name}
  const agentMatch = path.match(/^(.*\/)?\.agents\/([a-z0-9][a-z0-9_-]*)$/)
  if (agentMatch) return `${agentMatch[1] ?? ''}.agents/${agentMatch[2]}/agent.md`
  // page path
  return `${path}/content.md`
}

function filePathToHash(fp: string): string {
  if (fp === 'content.md') return ''
  const agentMatch = fp.match(/^(.*\/)?\.agents\/([a-z0-9][a-z0-9_-]*)\/agent\.md$/)
  if (agentMatch) return `#/${agentMatch[1] ?? ''}.agents/${agentMatch[2]}`
  const pageMatch = fp.match(/^(.+)\/content\.md$/)
  if (pageMatch) return `#/${pageMatch[1]}`
  return ''
}

function getBreadcrumbs(fp: string): BreadcrumbSegment[] {
  const crumbs: BreadcrumbSegment[] = [{ label: 'Home', filePath: 'content.md' }]
  if (fp === 'content.md') return crumbs

  const agentMatch = fp.match(/^(.*\/)?\.agents\/([a-z0-9][a-z0-9_-]*)\/agent\.md$/)
  if (agentMatch) {
    const pagePart = agentMatch[1] ? agentMatch[1].replace(/\/$/, '') : null
    if (pagePart) {
      let acc = ''
      for (const seg of pagePart.split('/')) {
        acc = acc ? `${acc}/${seg}` : seg
        crumbs.push({ label: seg, filePath: `${acc}/content.md` })
      }
    }
    crumbs.push({ label: '.agents', filePath: null })
    crumbs.push({ label: agentMatch[2], filePath: fp })
    return crumbs
  }

  const pageMatch = fp.match(/^(.+)\/content\.md$/)
  if (pageMatch) {
    let acc = ''
    for (const seg of pageMatch[1].split('/')) {
      acc = acc ? `${acc}/${seg}` : seg
      crumbs.push({ label: seg, filePath: `${acc}/content.md` })
    }
    return crumbs
  }

  return crumbs
}

// Derive the page path (used for listing page-scoped agents).
// "content.md" or ".agents/*/agent.md"  →  "" (root page)
// "{page}/content.md" or "{page}/.agents/*/agent.md"  →  "{page}"
function getPagePath(filePath: string): string {
  if (filePath === 'content.md' || filePath.startsWith('.agents/')) return ''
  return filePath.split('/').slice(0, -1).filter((s) => !s.startsWith('.')).join('/')
}

type Mode = 'view' | 'edit' | 'credentials'

export default function App() {
  const [session, setSession] = useState<Session | null | undefined>(undefined)
  const [filePath, setFilePath] = useState(getFilePath)
  const [content, setContent] = useState<string | null>(null)
  const [savedContent, setSavedContent] = useState<string>('')
  const [mode, setMode] = useState<Mode>('view')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [agents, setAgents] = useState<string[]>([])

  // Auth: subscribe to session changes; redirect to Google when there is no session
  // and no PKCE code exchange in progress.
  //
  // onAuthStateChange is the single source of truth for session state.
  // getSession() is called only to determine whether to trigger the OAuth redirect;
  // it must NOT call setSession because with PKCE the exchange is asynchronous —
  // onAuthStateChange may have already stored the real session by the time
  // getSession() resolves, and calling setSession(null) here would overwrite it.
  useEffect(() => {
    const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, s) => {
      setSession(s)
    })
    supabase.auth.getSession().then(({ data: { session: s } }) => {
      if (!s && !window.location.search.includes('code=')) {
        supabase.auth.signInWithOAuth({
          provider: 'google',
          options: { redirectTo: window.location.origin },
        })
      }
    })
    return () => subscription.unsubscribe()
  }, [])

  // Update filePath on hash navigation
  useEffect(() => {
    const handler = () => setFilePath(getFilePath())
    window.addEventListener('hashchange', handler)
    return () => window.removeEventListener('hashchange', handler)
  }, [])

  // Fetch the agents list for the current page whenever we enter edit mode
  // or navigate to a different page while already editing.
  useEffect(() => {
    if (!session || mode !== 'edit') return
    const pagePath = getPagePath(filePath)
    const url = `${API_BASE}/agents?site=${encodeURIComponent(SITE)}&page_path=${encodeURIComponent(pagePath)}`
    fetch(url, { headers: { 'Authorization': `Bearer ${session.access_token}` } })
      .then((res) => (res.ok ? res.json() : { agents: [] }))
      .then((data) => setAgents(data.agents ?? []))
      .catch(() => setAgents([]))
  }, [mode, filePath, session?.access_token])  // eslint-disable-line react-hooks/exhaustive-deps

  // Fetch the markdown file through the API (works in dev and production).
  useEffect(() => {
    if (!session) return
    setContent(null)
    setError(null)
    fetch(`${API_BASE}/content?site=${encodeURIComponent(SITE)}&path=${encodeURIComponent(filePath)}`, {
      headers: { 'Authorization': `Bearer ${session.access_token}` },
    })
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load content: ${res.status}`)
        return res.text()
      })
      .then((text) => {
        setContent(text)
        setSavedContent(text)
      })
      .catch((err) => setError(err.message))
  }, [filePath, session?.access_token])  // eslint-disable-line react-hooks/exhaustive-deps

  async function save() {
    if (content === null) return
    setSaving(true)
    try {
      const headers: Record<string, string> = { 'Content-Type': 'text/plain' }
      if (session?.access_token) headers['Authorization'] = `Bearer ${session.access_token}`
      const res = await fetch(
        `${API_BASE}/content?site=${encodeURIComponent(SITE)}&path=${encodeURIComponent(filePath)}`,
        {
          method: 'PUT',
          headers,
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

  function navigate(fp: string) {
    window.location.hash = filePathToHash(fp)
  }

  if (session === undefined) return <div className="state-center">Loading…</div>
  if (session === null) return null

  return (
    <>
      <header className="topbar">
        <span className="topbar-title">AgentScribe</span>
        <div className="topbar-actions">
          <button
            className={`btn${mode === 'credentials' ? ' btn-primary' : ''}`}
            onClick={() => setMode(mode === 'credentials' ? 'view' : 'credentials')}
          >
            {mode === 'credentials' ? '← Back' : 'Credentials'}
          </button>
          {mode === 'view' && (
            <button className="btn" onClick={() => setMode('edit')}>
              Edit
            </button>
          )}
          {mode === 'edit' && (
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

      <Breadcrumb segments={getBreadcrumbs(filePath)} onNavigate={navigate} />

      <div className="workspace">
        {mode === 'credentials' ? (
          <CredentialsPanel apiBase={API_BASE} token={session.access_token} />
        ) : (
          <>
            {mode === 'edit' && content !== null && (
              <ChatPanel
                content={content}
                onContentUpdate={setContent}
                apiBase={API_BASE}
                site={SITE}
                filePath={filePath}
                token={session.access_token}
              />
            )}

            {mode === 'edit' && (
              <AgentsList agents={agents} activeFilePath={filePath} />
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
          </>
        )}
      </div>
    </>
  )
}
