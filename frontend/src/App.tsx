import { useState, useEffect } from 'react'
import { authClient, type AuthSession } from './auth'
import MarkdownViewer from './components/MarkdownViewer'
import MarkdownEditor from './components/MarkdownEditor'
import ChatPanel from './components/ChatPanel'
import AgentsList from './components/AgentsList'
import Breadcrumb, { type BreadcrumbSegment } from './components/Breadcrumb'
import ToolsPanel from './components/ToolsPanel'
import SkillsPanel from './components/SkillsPanel'
import LandingPage from './components/LandingPage'
import OnboardingView from './components/OnboardingView'
import DeleteAccountModal from './components/DeleteAccountModal'
import CreatePageModal from './components/CreatePageModal'
import ChildPagesList from './components/ChildPagesList'
import PageSettingsPanel from './components/PageSettingsPanel'
import AccessDeniedView from './components/AccessDeniedView'
import TokensPanel from './components/TokensPanel'

type AccessLevel = 'full-control' | 'write' | 'view' | 'denied' | null

export interface AgentMeta {
  name: string
  trigger: 'manual' | 'schedule' | 'on_write' | string
  scope: string[]
  is_pointer: boolean
}

// In LOCAL_MODE Supabase auth is bypassed — the backend accepts all requests
// as the local user. A synthetic session is used so existing session-gated
// code paths continue to work without changes.
const LOCAL_MODE = import.meta.env.VITE_LOCAL_MODE === 'true'

const LOCAL_SESSION: AuthSession = {
  access_token: 'local',
  user: {
    id: 'local-user-00000000',
    email: 'local@localhost',
    user_metadata: { full_name: 'Local User' },
  },
}

// In dev mode always use the Vite proxy (/api → localhost:8000) regardless of
// any VITE_API_BASE shell variable that may be set from running the deploy script.
// In production the build sets VITE_API_BASE to the ALB URL at build time.
const API_BASE = import.meta.env.DEV ? '/api' : (import.meta.env.VITE_API_BASE || '/api')

// Derive the site name from the first URL path segment so the frontend always
// knows which S3 prefix it is operating under.
// Production: /knuth-home/  →  "knuth-home"
// Dev with no path segment  →  VITE_SITE env var, or "default"
function getSite(): string {
  const first = window.location.pathname.split('/').filter(Boolean)[0]
  return first ?? import.meta.env.VITE_SITE ?? 'default'
}

const SITE = getSite()
const IS_MAIN_SITE = SITE === 'default'

// ── Hash ↔ filePath helpers ────────────────────────────────────────────────────
//
// Hash scheme:
//   (empty)                      →  content.md              (root page)
//   #/{page}                     →  {page}/content.md       (child page)
//   #/.agents/{name}             →  .agents/{name}/agent.md (root agent)
//   #/{page}/.agents/{name}      →  {page}/.agents/{name}/agent.md
//   #/.skills/{name}             →  .skills/{name}/SKILL.md (site skill)

function getFilePath(): string {
  const hash = window.location.hash
  if (!hash) return 'content.md'
  // Supabase implicit-flow callback: ignore auth fragments before the client strips them
  if (hash.includes('access_token=') || hash.includes('error_description=')) return 'content.md'
  const path = hash.replace(/^#\/?/, '')
  if (!path) return 'content.md'
  // .user/search
  if (path === '.user/search') return '.user/search.md'
  // .user/notifications
  if (path === '.user/notifications') return '.user/notifications.md'
  // .skills/{name}
  const skillMatch = path.match(/^\.skills\/([a-z0-9][a-z0-9_-]*)$/)
  if (skillMatch) return `.skills/${skillMatch[1]}/SKILL.md`
  // {page}/.agents/{name}  or  .agents/{name}
  const agentMatch = path.match(/^(.*\/)?\.agents\/([a-z0-9][a-z0-9_-]*)$/)
  if (agentMatch) return `${agentMatch[1] ?? ''}.agents/${agentMatch[2]}/agent.md`
  // page path
  return `${path}/content.md`
}

function filePathToHash(fp: string): string {
  if (fp === 'content.md') return ''
  if (fp === '.user/search.md') return '#/.user/search'
  if (fp === '.user/notifications.md') return '#/.user/notifications'
  const skillMatch = fp.match(/^\.skills\/([a-z0-9][a-z0-9_-]*)\/SKILL\.md$/)
  if (skillMatch) return `#/.skills/${skillMatch[1]}`
  const agentMatch = fp.match(/^(.*\/)?\.agents\/([a-z0-9][a-z0-9_-]*)\/agent\.md$/)
  if (agentMatch) return `#/${agentMatch[1] ?? ''}.agents/${agentMatch[2]}`
  const pageMatch = fp.match(/^(.+)\/content\.md$/)
  if (pageMatch) return `#/${pageMatch[1]}`
  return ''
}

function getBreadcrumbs(fp: string): BreadcrumbSegment[] {
  const crumbs: BreadcrumbSegment[] = [{ label: 'Home', filePath: 'content.md' }]
  if (fp === 'content.md') return crumbs

  if (fp === '.user/search.md') {
    crumbs.push({ label: 'Search Results', filePath: fp })
    return crumbs
  }

  if (fp === '.user/notifications.md') {
    crumbs.push({ label: 'Notifications', filePath: fp })
    return crumbs
  }

  const skillMatch = fp.match(/^\.skills\/([a-z0-9][a-z0-9_-]*)\/SKILL\.md$/)
  if (skillMatch) {
    crumbs.push({ label: '.skills', filePath: null })
    crumbs.push({ label: skillMatch[1], filePath: fp })
    return crumbs
  }

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
  // Child page content: "{page}/content.md"
  if (filePath.endsWith('/content.md')) return filePath.slice(0, -'/content.md'.length)
  // Child page agent: "{page}/.agents/{name}/agent.md"
  // Everything before the first "/.agents/" segment is the page path.
  const agentsIdx = filePath.indexOf('/.agents/')
  if (agentsIdx !== -1) return filePath.slice(0, agentsIdx)
  return ''
}

type Mode = 'view' | 'edit' | 'tools'
type AppView = 'loading' | 'landing' | 'onboarding' | 'site'

export default function App() {
  const [filePath, setFilePath] = useState(getFilePath)
  const [content, setContent] = useState<string | null>(null)
  const [savedContent, setSavedContent] = useState<string>('')
  const [mode, setMode] = useState<Mode>('view')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveConflict, setSaveConflict] = useState(false)
  const [etag, setEtag] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const [agents, setAgents] = useState<AgentMeta[]>([])
  const [session, setSession] = useState<AuthSession | null | undefined>(undefined)
  const [avatarOpen, setAvatarOpen] = useState(false)
  const [deleteModalOpen, setDeleteModalOpen] = useState(false)
  const [createPageOpen, setCreatePageOpen] = useState(false)
  const [mySite, setMySite] = useState<string | null | undefined>(undefined)
  const [accessLevel, setAccessLevel] = useState<AccessLevel>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [skillsOpen, setSkillsOpen] = useState(false)
  const [tokensOpen, setTokensOpen] = useState(false)
  const [hasNotifications, setHasNotifications] = useState(false)

  // Subscribe to auth state changes (skipped in LOCAL_MODE).
  useEffect(() => {
    if (LOCAL_MODE) {
      setSession(LOCAL_SESSION)
      return
    }
    const unsubscribe = authClient.onAuthStateChange((s) => {
      setSession(s)
    })
    return unsubscribe
  }, [])

  // Fetch CloudFront signed cookies on session establish and refresh at 55 min
  // so video/audio assets remain accessible without interruption (YOL-129).
  // In LOCAL_MODE the backend returns 200 with no cookies — the call is harmless
  // and keeps the code path exercised locally.
  useEffect(() => {
    if (!session) return

    function fetchMediaAuth() {
      fetch(`${API_BASE}/media-auth`, {
        credentials: 'include',   // cookies must be sent cross-origin to CloudFront domain
        headers: { Authorization: `Bearer ${session!.access_token}` },
      }).catch(() => {/* best-effort; never block rendering */})
    }

    fetchMediaAuth()
    // Re-fetch 5 minutes before the 1-hour cookie TTL expires.
    const id = setInterval(fetchMediaAuth, 55 * 60 * 1000)
    return () => clearInterval(id)
  }, [session?.user.id])  // re-run only when the user identity changes, not on token refresh

  // Load site theme from config.json (user sites only)
  useEffect(() => {
    if (IS_MAIN_SITE) return
    fetch(`${API_BASE}/content?site=${encodeURIComponent(SITE)}&path=config.json`)
      .then((res) => res.ok ? res.json() : null)
      .then((data) => {
        if (data?.theme) document.documentElement.setAttribute('data-theme', data.theme)
      })
      .catch(() => {})
  }, [])

  // On main site: after login, fetch /my-site to decide onboarding vs redirect
  useEffect(() => {
    if (!IS_MAIN_SITE) return
    if (session === undefined || session === null) {
      setMySite(undefined)
      return
    }
    fetch(`${API_BASE}/my-site`, {
      headers: { Authorization: `Bearer ${session.access_token}` },
    })
      .then((res) => res.ok ? res.json() : { site_name: null })
      .then((data) => {
        const name: string | null = data.site_name ?? null
        if (name) {
          window.location.href = `/${name}`
        } else {
          setMySite(null)
        }
      })
      .catch(() => setMySite(null))
  }, [session])

  // Determine which view to show.
  // Non-main sites no longer show a blanket auth-wall when unauthenticated —
  // public pages are accessible without login, and the content fetch sets
  // accessLevel to 'denied' when the page requires authentication.
  const appView: AppView = (() => {
    if (session === undefined) return 'loading'
    if (session === null && IS_MAIN_SITE) return 'landing'
    if (IS_MAIN_SITE) {
      if (mySite === undefined) return 'loading'
      return 'onboarding'
    }
    return 'site'
  })()

  function signIn() {
    if (LOCAL_MODE) return
    authClient.signIn()
  }

  function signOut() {
    if (LOCAL_MODE) return
    authClient.signOut()
  }

  // Update filePath on hash navigation
  useEffect(() => {
    const handler = () => setFilePath(getFilePath())
    window.addEventListener('hashchange', handler)
    return () => window.removeEventListener('hashchange', handler)
  }, [])

  // Fetch the agents list for the current page whenever we enter edit mode
  // or navigate to a different page while already editing.
  useEffect(() => {
    if (mode !== 'edit') return
    const pagePath = getPagePath(filePath)
    const url = `${API_BASE}/agents?site=${encodeURIComponent(SITE)}&page_path=${encodeURIComponent(pagePath)}`
    fetch(url)
      .then((res) => (res.ok ? res.json() : { agents: [] }))
      .then((data) => setAgents(data.agents ?? []))
      .catch(() => setAgents([]))
  }, [mode, filePath])

  // Stable identity key: re-fetch when page changes or when user logs in/out,
  // but NOT on every token refresh (which changes access_token but not user.id).
  const sessionUserId = session?.user.id ?? null
  // true once onAuthStateChange has delivered the first callback (session is no
  // longer undefined).  We gate the content fetch on this so we never send an
  // unauthenticated request for a private page before the session is known.
  const authReady = session !== undefined

  // Fetch the markdown file through the API (works in dev and production).
  useEffect(() => {
    if (!authReady) return   // wait for auth state to resolve before fetching
    const controller = new AbortController()
    setContent(null)
    setError(null)
    setAccessLevel(null)
    setMode('view')
    setEtag(null)
    setSaveConflict(false)
    const headers: Record<string, string> = {}
    if (session?.access_token) headers['Authorization'] = `Bearer ${session.access_token}`
    fetch(`${API_BASE}/content?site=${encodeURIComponent(SITE)}&path=${encodeURIComponent(filePath)}`, {
      signal: controller.signal,
      headers,
    })
      .then(async (res) => {
        if (res.status === 403) {
          setAccessLevel('denied')
          setContent('')
          return
        }
        if (!res.ok) throw new Error(`Failed to load content: ${res.status}`)
        const level = (res.headers.get('X-Page-Access') ?? 'view') as AccessLevel
        setAccessLevel(level)
        setEtag(res.headers.get('ETag'))
        const text = await res.text()
        setContent(text)
        setSavedContent(text)
      })
      .catch((err) => { if (err.name !== 'AbortError') setError(err.message) })
    return () => controller.abort()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filePath, sessionUserId, authReady, reloadKey])

  const isOwner = accessLevel === 'full-control'
  const canEdit = accessLevel === 'full-control' || accessLevel === 'write'
  const canRunAgents = accessLevel === 'full-control'

  // Poll notifications badge when the user is the site owner
  useEffect(() => {
    if (!isOwner || !session) return
    fetch(
      `${API_BASE}/content?site=${encodeURIComponent(SITE)}&path=${encodeURIComponent('.user/notifications.md')}`,
      { headers: { Authorization: `Bearer ${session.access_token}` } },
    )
      .then((res) => (res.ok ? res.text() : ''))
      .then((text) => setHasNotifications(text.trim().length > 0))
      .catch(() => {})
  }, [isOwner, session, filePath])

  async function save(force = false) {
    if (content === null || !session) return
    setSaving(true)
    setSaveConflict(false)
    try {
      const headers: Record<string, string> = {
        'Content-Type': 'text/plain',
        Authorization: `Bearer ${session.access_token}`,
      }
      if (etag && !force) headers['If-Match'] = etag
      const res = await fetch(
        `${API_BASE}/content?site=${encodeURIComponent(SITE)}&path=${encodeURIComponent(filePath)}`,
        { method: 'PUT', headers, body: content }
      )
      if (res.status === 409) {
        setSaveConflict(true)
        return
      }
      if (!res.ok) throw new Error(`Save failed: ${res.status}`)
      setSavedContent(content)
      setEtag(null) // backend doesn't return ETag on PUT; clear so next save is unconditional
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

  // ── View rendering ────────────────────────────────────────────────────────

  if (appView === 'loading') {
    return <div className="state-center" style={{ height: '100vh' }}>Loading…</div>
  }

  if (appView === 'landing') {
    return <LandingPage onSignIn={signIn} />
  }

  if (appView === 'onboarding') {
    return (
      <OnboardingView
        apiBase={API_BASE}
        token={session!.access_token}
        defaultSiteName={(session!.user.email ?? '').split('@')[0]}
        onSuccess={(siteName) => { window.location.href = `/${siteName}` }}
      />
    )
  }


  // appView === 'site'
  const isContentPage = filePath === 'content.md' || filePath.endsWith('/content.md')

  return (
    <>
      <header className="topbar">
        <span className="topbar-title">Yolo Scribe</span>
        <div className="topbar-actions">
          {isContentPage && mode !== 'tools' && !skillsOpen && (
            <button className="btn" onClick={() => setCreatePageOpen(true)}>
              + New Page
            </button>
          )}
          {canEdit && mode === 'view' && !skillsOpen && (
            <button className="btn" onClick={() => setMode('edit')}>
              Edit
            </button>
          )}
          {canEdit && mode === 'edit' && (
            <>
              <button className="btn btn-danger" onClick={discard}>
                Discard
              </button>
              {saveConflict && (
                <>
                  <span style={{ color: 'var(--danger, #e53e3e)', fontSize: '0.85em' }}>
                    Conflict — page changed by another writer.
                  </span>
                  <button className="btn btn-danger" onClick={() => save(true)}>
                    Force Save
                  </button>
                  <button className="btn" onClick={() => { setSaveConflict(false); setReloadKey((k) => k + 1) }}>
                    Reload
                  </button>
                </>
              )}
              {!saveConflict && (
                <button
                  className="btn btn-primary"
                  onClick={() => save()}
                  disabled={!isDirty || saving}
                >
                  {saving ? 'Saving…' : 'Save'}
                </button>
              )}
              <button className="btn" onClick={() => setMode('view')}>
                View
              </button>
            </>
          )}
          {isOwner && (
            <button
              className={`btn${mode === 'tools' ? ' btn-primary' : ''}`}
              onClick={() => { setMode(mode === 'tools' ? 'view' : 'tools'); setSkillsOpen(false); setTokensOpen(false) }}
            >
              Tools
            </button>
          )}
          {isOwner && (
            <button
              className={`btn${skillsOpen ? ' btn-primary' : ''}`}
              onClick={() => { setSkillsOpen((o) => !o); setMode('view'); setTokensOpen(false) }}
            >
              {skillsOpen ? 'Back' : 'Skills'}
            </button>
          )}
          {isOwner && (
            <button
              className={`btn${tokensOpen ? ' btn-primary' : ''}`}
              onClick={() => { setTokensOpen((o) => !o); setMode('view'); setSkillsOpen(false) }}
            >
              {tokensOpen ? 'Back' : 'API Tokens'}
            </button>
          )}
          {isOwner && isContentPage && mode !== 'tools' && !skillsOpen && (
            <button
              className={`btn${settingsOpen ? ' btn-primary' : ''}`}
              onClick={() => setSettingsOpen((o) => !o)}
            >
              Settings
            </button>
          )}
          {isOwner && (
            <button
              className="btn"
              style={{ position: 'relative' }}
              onClick={() => navigate('.user/notifications.md')}
              title="Notifications"
            >
              🔔{hasNotifications && (
                <span style={{
                  position: 'absolute', top: 2, right: 2,
                  width: 8, height: 8, borderRadius: '50%',
                  background: 'var(--danger, #e53e3e)',
                  display: 'inline-block',
                }} />
              )}
            </button>
          )}
          {LOCAL_MODE ? (
            <span className="btn" style={{ opacity: 0.6, cursor: 'default', pointerEvents: 'none' }}>
              Local
            </span>
          ) : session ? (
            <div className="auth-avatar-wrap">
              <button className="auth-avatar" onClick={() => setAvatarOpen((o) => !o)}>
                {(session.user.user_metadata?.full_name ?? session.user.email ?? '?')[0].toUpperCase()}
              </button>
              {avatarOpen && (
                <>
                  <div className="auth-overlay" onClick={() => setAvatarOpen(false)} />
                  <div className="auth-avatar-menu">
                    <div className="auth-avatar-email">{session.user.email}</div>
                    <button className="btn" onClick={signOut}>Sign out</button>
                    {window.location.hostname === 'localhost' && (
                      <button
                        className="btn"
                        onClick={() => {
                          navigator.clipboard.writeText(session.access_token)
                          setAvatarOpen(false)
                        }}
                      >
                        Copy JWT
                      </button>
                    )}
                    {isOwner && (
                      <button
                        className="btn btn-danger"
                        onClick={() => { setAvatarOpen(false); setDeleteModalOpen(true) }}
                      >
                        Delete Account
                      </button>
                    )}
                  </div>
                </>
              )}
            </div>
          ) : (
            <button className="btn" onClick={signIn}>Sign in</button>
          )}
        </div>
      </header>
      {deleteModalOpen && session && (
        <DeleteAccountModal
          apiBase={API_BASE}
          token={session.access_token}
          onClose={() => setDeleteModalOpen(false)}
          onDeleted={async () => {
            await authClient.signOut()
            window.location.href = '/'
          }}
        />
      )}
      {createPageOpen && session && (
        <CreatePageModal
          apiBase={API_BASE}
          site={SITE}
          token={session.access_token}
          parentPagePath={getPagePath(filePath)}
          onSuccess={(pagePath) => {
            setCreatePageOpen(false)
            navigate(`${pagePath}/content.md`)
          }}
          onClose={() => setCreatePageOpen(false)}
        />
      )}

      <Breadcrumb segments={getBreadcrumbs(filePath)} onNavigate={navigate} />

      <div className="workspace">
        {accessLevel === 'denied' ? (
          <div className="content-area">
            <AccessDeniedView
              isAuthenticated={!!session}
              onSignIn={signIn}
              onRequestAccess={async () => {
                if (!session) return
                await fetch(`${API_BASE}/request-access`, {
                  method: 'POST',
                  headers: {
                    'Content-Type': 'application/json',
                    Authorization: `Bearer ${session.access_token}`,
                  },
                  body: JSON.stringify({ site: SITE, path: filePath }),
                })
              }}
            />
          </div>
        ) : mode === 'tools' && isOwner ? (
          <ToolsPanel apiBase={API_BASE} token={session!.access_token} site={SITE} />
        ) : skillsOpen && isOwner ? (
          <SkillsPanel apiBase={API_BASE} site={SITE} token={session!.access_token} />
        ) : tokensOpen && isOwner ? (
          <TokensPanel apiBase={API_BASE} token={session!.access_token} />
        ) : settingsOpen && isOwner ? (
          <div className="content-area">
            <PageSettingsPanel
              apiBase={API_BASE}
              site={SITE}
              filePath={filePath}
              token={session!.access_token}
              onClose={() => setSettingsOpen(false)}
            />
          </div>
        ) : (
          <>
            {canRunAgents && mode === 'edit' && content !== null && (
              <ChatPanel
                content={content}
                onContentUpdate={setContent}
                apiBase={API_BASE}
                site={SITE}
                filePath={filePath}
                token={session!.access_token}
              />
            )}

            {canRunAgents && mode === 'edit' && (
              <AgentsList
                agents={agents}
                activeFilePath={filePath}
                pagePath={getPagePath(filePath)}
                apiBase={API_BASE}
                site={SITE}
                token={session!.access_token}
                onAgentsChanged={() => {
                  const pagePath = getPagePath(filePath)
                  const url = `${API_BASE}/agents?site=${encodeURIComponent(SITE)}&page_path=${encodeURIComponent(pagePath)}`
                  fetch(url)
                    .then((res) => (res.ok ? res.json() : { agents: [] }))
                    .then((data) => setAgents(data.agents ?? []))
                    .catch(() => setAgents([]))
                }}
              />
            )}

            <div className="content-area">
              {error ? (
                <div className="state-center">{error}</div>
              ) : content === null ? (
                <div className="state-center">Loading…</div>
              ) : mode === 'view' ? (
                <div className="view-scroll">
                  <MarkdownViewer content={content} site={SITE} apiBase={API_BASE} />
                  {isContentPage && (
                    <ChildPagesList
                      apiBase={API_BASE}
                      site={SITE}
                      pagePath={getPagePath(filePath)}
                      onNavigate={navigate}
                    />
                  )}
                </div>
              ) : (
                <MarkdownEditor
                  content={content}
                  onChange={setContent}
                  isOwner={isOwner}
                  site={SITE}
                  apiBase={API_BASE}
                  token={session?.access_token}
                  pagePath={getPagePath(filePath)}
                />
              )}
            </div>
          </>
        )}
      </div>
    </>
  )
}
