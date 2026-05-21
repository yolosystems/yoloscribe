import { useState, useEffect, useRef } from 'react'
import OutboundWebhooksPanel from './OutboundWebhooksPanel'

// ── API response types ────────────────────────────────────────────────────────

interface OAuthTool {
  type: 'oauth'
  enabled: boolean
  authenticated: boolean
  expires_at: string | null
  scope: string | null
}

interface KeyTool {
  type: 'key'
  enabled: boolean
  vars: string[]
  stored: Record<string, boolean>
}

interface AwsSsoTool {
  type: 'aws-sso'
  enabled: boolean
  configured: boolean
  sso_start_url: string | null
  sso_region: string | null
  aws_region: string | null
  authenticated: boolean
  account_id: string | null
  role_name: string | null
  expires_at: string | null
}

interface NoneTool {
  type: 'none'
  enabled: boolean
}

type ToolStatus = OAuthTool | KeyTool | AwsSsoTool | NoneTool

interface ToolsResponse {
  tools: Record<string, ToolStatus>
}

interface Props {
  apiBase: string
  token: string
  site: string
}

// ── SSO device-authorization flow state ───────────────────────────────────────

interface SsoFlow {
  session: string
  user_code: string
  polling_interval: number
  status: 'waiting' | 'selecting-account' | 'selecting-role'
  accounts: Array<{ account_id: string; account_name: string; email: string }>
  selected_account_id: string | null
  roles: Array<{ role_name: string }>
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function ToolsPanel({ apiBase, token, site }: Props) {
  const [status, setStatus] = useState<ToolsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Key-based tool state
  const [inputs, setInputs] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState<Record<string, boolean>>({})
  const [flashSaved, setFlashSaved] = useState<Record<string, boolean>>({})
  // OAuth flow state
  const [initiating, setInitiating] = useState<Record<string, boolean>>({})
  // Enable/disable toggle state
  const [toggling, setToggling] = useState<Record<string, boolean>>({})
  const [oauthBanner, setOAuthBanner] = useState<{ tool: string; kind: 'success' | 'error'; message: string } | null>(null)
  // AWS SSO setup form
  const [ssoSetup, setSsoSetup] = useState<{ start_url: string; region: string; aws_region: string }>({ start_url: '', region: 'us-east-1', aws_region: '' })
  const [savingSsoSetup, setSavingSsoSetup] = useState(false)
  // AWS SSO device-authorization flow
  const [ssoFlow, setSsoFlow] = useState<SsoFlow | null>(null)
  const [ssoError, setSsoError] = useState<string | null>(null)
  const ssoPollingRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Cleanup polling timer on unmount
  useEffect(() => {
    return () => {
      if (ssoPollingRef.current) clearTimeout(ssoPollingRef.current)
    }
  }, [])

  // On mount: check if we're returning from an OAuth redirect
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const successTool = params.get('oauth_success')
    const errorMsg = params.get('oauth_error')

    if (successTool || errorMsg) {
      params.delete('oauth_success')
      params.delete('oauth_error')
      const newSearch = params.toString()
      window.history.replaceState({}, '', window.location.pathname + (newSearch ? '?' + newSearch : ''))

      if (errorMsg) {
        setOAuthBanner({ tool: '', kind: 'error', message: decodeURIComponent(errorMsg) })
      } else if (successTool) {
        setOAuthBanner({ tool: successTool, kind: 'success', message: `Successfully authenticated ${successTool}.` })
      }
    }

    loadStatus()
  }, [apiBase])

  function loadStatus() {
    fetch(`${apiBase}/tools?site=${encodeURIComponent(site)}`, {
      headers: { ...(token && { Authorization: `Bearer ${token}` }) },
    })
      .then((res) => (res.ok ? res.json() : Promise.reject(`HTTP ${res.status}`)))
      .then((data: ToolsResponse) => {
        setStatus(data)
        // Pre-populate SSO setup form from the first aws-sso tool's current config
        const ssoTool = Object.values(data.tools).find((t): t is AwsSsoTool => t.type === 'aws-sso')
        if (ssoTool && (ssoTool.sso_start_url || ssoTool.sso_region)) {
          setSsoSetup({
            start_url: ssoTool.sso_start_url ?? '',
            region: ssoTool.sso_region ?? 'us-east-1',
            aws_region: ssoTool.aws_region ?? '',
          })
        }
      })
      .catch((e) => setError(`Failed to load tools: ${e}`))
  }

  // ── Enable / disable toggle ─────────────────────────────────────────────────

  async function toggleEnabled(toolName: string, enable: boolean) {
    setToggling((prev) => ({ ...prev, [toolName]: true }))
    try {
      const action = enable ? 'enable' : 'disable'
      const res = await fetch(
        `${apiBase}/tools/${encodeURIComponent(toolName)}/${action}?site=${encodeURIComponent(site)}`,
        {
          method: 'POST',
          headers: { ...(token && { Authorization: `Bearer ${token}` }) },
        }
      )
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setStatus((prev) => {
        if (!prev) return prev
        return { tools: { ...prev.tools, [toolName]: { ...prev.tools[toolName], enabled: enable } } }
      })
    } catch (e) {
      alert(`Failed to ${enable ? 'enable' : 'disable'} ${toolName}: ${e instanceof Error ? e.message : e}`)
    } finally {
      setToggling((prev) => ({ ...prev, [toolName]: false }))
    }
  }

  // ── OAuth flow (non-SSO tools) ───────────────────────────────────────────────

  async function startOAuth(toolName: string) {
    setInitiating((prev) => ({ ...prev, [toolName]: true }))
    try {
      const res = await fetch(`${apiBase}/oauth/initiate/${encodeURIComponent(toolName)}`, {
        method: 'POST',
        headers: { ...(token && { Authorization: `Bearer ${token}` }) },
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail ?? `HTTP ${res.status}`)
      }
      const { auth_url } = await res.json()
      window.location.href = auth_url
    } catch (e) {
      setInitiating((prev) => ({ ...prev, [toolName]: false }))
      setOAuthBanner({ tool: toolName, kind: 'error', message: `Failed to start OAuth for ${toolName}: ${e}` })
    }
  }

  // ── AWS SSO device-authorization flow ───────────────────────────────────────

  async function startAwsSso() {
    setSsoError(null)
    setInitiating((prev) => ({ ...prev, 'aws-sso': true }))
    try {
      const res = await fetch(`${apiBase}/oauth/initiate/aws-sso`, {
        method: 'POST',
        headers: { ...(token && { Authorization: `Bearer ${token}` }) },
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail ?? `HTTP ${res.status}`)
      }
      const { auth_url, user_code, session, polling_interval } = await res.json()
      window.open(auth_url, '_blank', 'noopener,noreferrer')
      setSsoFlow({
        session,
        user_code,
        polling_interval,
        status: 'waiting',
        accounts: [],
        selected_account_id: null,
        roles: [],
      })
      // Start polling after the first interval
      ssoPollingRef.current = setTimeout(() => pollSsoStatus(session, polling_interval), polling_interval * 1000)
    } catch (e) {
      setSsoError(`Failed to start AWS SSO: ${e instanceof Error ? e.message : e}`)
    } finally {
      setInitiating((prev) => ({ ...prev, 'aws-sso': false }))
    }
  }

  async function pollSsoStatus(session: string, pollingInterval: number) {
    ssoPollingRef.current = null
    try {
      const res = await fetch(
        `${apiBase}/aws-sso/auth-status?session=${encodeURIComponent(session)}`,
        { headers: { ...(token && { Authorization: `Bearer ${token}` }) } }
      )
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()

      if (data.status === 'pending') {
        ssoPollingRef.current = setTimeout(() => pollSsoStatus(session, pollingInterval), pollingInterval * 1000)
      } else if (data.status === 'authorized') {
        setSsoFlow((prev) => prev ? { ...prev, status: 'selecting-account', accounts: data.accounts } : null)
      } else if (data.status === 'expired') {
        setSsoFlow(null)
        setSsoError('AWS SSO session expired. Please try again.')
      } else {
        setSsoFlow(null)
        setSsoError(`AWS SSO error: ${data.error ?? 'Unknown error'}`)
      }
    } catch (e) {
      setSsoFlow(null)
      setSsoError(`Failed to check SSO status: ${e instanceof Error ? e.message : e}`)
    }
  }

  function cancelSsoFlow() {
    if (ssoPollingRef.current) {
      clearTimeout(ssoPollingRef.current)
      ssoPollingRef.current = null
    }
    setSsoFlow(null)
  }

  async function selectSsoAccount(accountId: string) {
    setSsoFlow((prev) => prev ? { ...prev, status: 'selecting-role', selected_account_id: accountId, roles: [] } : null)
    try {
      const res = await fetch(
        `${apiBase}/aws-sso/roles/${encodeURIComponent(accountId)}`,
        { headers: { ...(token && { Authorization: `Bearer ${token}` }) } }
      )
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const { roles } = await res.json()
      setSsoFlow((prev) => prev ? { ...prev, roles } : null)
    } catch (e) {
      setSsoFlow(null)
      setSsoError(`Failed to load roles: ${e instanceof Error ? e.message : e}`)
    }
  }

  async function selectSsoRole(roleName: string) {
    if (!ssoFlow?.selected_account_id) return
    try {
      const res = await fetch(`${apiBase}/aws-sso/select-role`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(token && { Authorization: `Bearer ${token}` }) },
        body: JSON.stringify({ account_id: ssoFlow.selected_account_id, role_name: roleName }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setSsoFlow(null)
      loadStatus()
    } catch (e) {
      setSsoFlow(null)
      setSsoError(`Failed to select role: ${e instanceof Error ? e.message : e}`)
    }
  }

  // ── AWS SSO setup save ──────────────────────────────────────────────────────

  async function saveAwsSsoSetup() {
    const start_url = ssoSetup.start_url.trim()
    const region = ssoSetup.region.trim() || 'us-east-1'
    const aws_region = ssoSetup.aws_region.trim() || region
    if (!start_url) return
    setSavingSsoSetup(true)
    try {
      const res = await fetch(`${apiBase}/aws-sso/setup?site=${encodeURIComponent(site)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...(token && { Authorization: `Bearer ${token}` }) },
        body: JSON.stringify({ sso_start_url: start_url, sso_region: region, aws_region }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      loadStatus()
    } catch (e) {
      alert(`Failed to save SSO setup: ${e instanceof Error ? e.message : e}`)
    } finally {
      setSavingSsoSetup(false)
    }
  }

  // ── Key-based credential save ───────────────────────────────────────────────

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
      setStatus((prev) => {
        if (!prev) return prev
        const nextTools: Record<string, ToolStatus> = {}
        for (const [name, data] of Object.entries(prev.tools)) {
          if (data.type === 'key' && varName in data.stored) {
            nextTools[name] = { ...data, stored: { ...data.stored, [varName]: true } }
          } else {
            nextTools[name] = data
          }
        }
        return { tools: nextTools }
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

  // ── Render ──────────────────────────────────────────────────────────────────

  if (error) return <div className="state-center">{error}</div>
  if (!status) return <div className="state-center">Loading…</div>

  const tools = Object.entries(status.tools)

  return (
    <div className="credentials-panel">
      <div className="credentials-header">Tools</div>
      <div className="credentials-body">
        {oauthBanner && (
          <div className={`credentials-banner credentials-banner--${oauthBanner.kind}`}>
            {oauthBanner.message}
            <button className="credentials-banner-dismiss" onClick={() => setOAuthBanner(null)}>✕</button>
          </div>
        )}
        {tools.length === 0 && (
          <p className="credentials-empty">No tools configured on this server.</p>
        )}
        {tools.map(([toolName, tool]) => (
          <div key={toolName} className="tool-card">
            {/* ── Card header ── */}
            <div className="tool-card-header">
              <span className="tool-card-name">{toolName}</span>
              <button
                className={`btn tool-toggle-btn${tool.enabled ? ' btn-primary' : ''}`}
                onClick={() => toggleEnabled(toolName, !tool.enabled)}
                disabled={toggling[toolName]}
              >
                {toggling[toolName] ? '…' : tool.enabled ? 'Enabled' : 'Disabled'}
              </button>
            </div>

            {/* ── Card body ── */}
            <div className="tool-card-body">
              {tool.type === 'oauth' ? (
                // ── OAuth tool ──────────────────────────────────────────────
                <>
                  <div className="tool-auth-status">
                    {tool.authenticated ? (
                      <span className="credentials-badge credentials-badge--stored">Authenticated ✓</span>
                    ) : (
                      <span className="credentials-badge credentials-badge--missing">Not authenticated</span>
                    )}
                    {tool.authenticated && (
                      <div className="tool-auth-meta">
                        {tool.expires_at && <span>Expires {new Date(tool.expires_at).toLocaleDateString()}</span>}
                        {tool.scope && <span>Scope: {tool.scope}</span>}
                      </div>
                    )}
                  </div>
                  <button
                    className="btn btn-primary"
                    onClick={() => startOAuth(toolName)}
                    disabled={initiating[toolName]}
                  >
                    {initiating[toolName]
                      ? 'Redirecting…'
                      : tool.authenticated
                      ? 'Re-authenticate'
                      : 'Authenticate via OAuth'}
                  </button>
                </>
              ) : tool.type === 'aws-sso' ? (
                // ── AWS SSO tool ────────────────────────────────────────────
                <>
                  {/* SSO config subsection */}
                  <div className="tool-section">
                    <div className="tool-section-label">Configuration</div>
                    <div className="tool-section-fields">
                      <input
                        className="credentials-input"
                        placeholder="SSO start URL (e.g. https://my-org.awsapps.com/start)"
                        value={ssoSetup.start_url}
                        onChange={(e) => setSsoSetup((p) => ({ ...p, start_url: e.target.value }))}
                      />
                      <input
                        className="credentials-input"
                        placeholder="SSO region (e.g. us-east-1)"
                        value={ssoSetup.region}
                        onChange={(e) => setSsoSetup((p) => ({ ...p, region: e.target.value }))}
                      />
                      <input
                        className="credentials-input"
                        placeholder="AWS resource region (e.g. us-west-2 — leave blank to use SSO region)"
                        value={ssoSetup.aws_region}
                        onChange={(e) => setSsoSetup((p) => ({ ...p, aws_region: e.target.value }))}
                      />
                      <button
                        className="btn btn-primary"
                        onClick={saveAwsSsoSetup}
                        disabled={savingSsoSetup || !ssoSetup.start_url.trim()}
                      >
                        {savingSsoSetup ? 'Saving…' : 'Save'}
                      </button>
                    </div>
                  </div>

                  {/* SSO auth subsection */}
                  {tool.configured && (
                    <div className="tool-section">
                      <div className="tool-section-label">Authentication</div>

                      {ssoError && (
                        <div className="credentials-banner credentials-banner--error">
                          {ssoError}
                          <button className="credentials-banner-dismiss" onClick={() => setSsoError(null)}>✕</button>
                        </div>
                      )}

                      <div className="tool-auth-status">
                        {tool.authenticated ? (
                          <span className="credentials-badge credentials-badge--stored">Signed in ✓</span>
                        ) : (
                          <span className="credentials-badge credentials-badge--missing">Not signed in</span>
                        )}
                        {tool.authenticated && (
                          <div className="tool-auth-meta">
                            {tool.account_id && <span>Account: {tool.account_id}</span>}
                            {tool.role_name && <span>Role: {tool.role_name}</span>}
                            {tool.expires_at && <span>Expires {new Date(tool.expires_at).toLocaleDateString()}</span>}
                          </div>
                        )}
                      </div>

                      {ssoFlow ? (
                        <div className="tool-sso-flow">
                          {ssoFlow.status === 'waiting' && (
                            <>
                              <p className="tool-sso-hint">
                                AWS SSO sign-in opened in a new tab. Confirm this code:
                              </p>
                              <div className="tool-sso-code">{ssoFlow.user_code}</div>
                              <p className="tool-sso-waiting">Waiting for approval…</p>
                              <button className="btn" onClick={cancelSsoFlow}>Cancel</button>
                            </>
                          )}
                          {ssoFlow.status === 'selecting-account' && (
                            <>
                              <p className="tool-sso-hint">Select an AWS account:</p>
                              {ssoFlow.accounts.map((acc) => (
                                <button
                                  key={acc.account_id}
                                  className="btn tool-sso-option"
                                  onClick={() => selectSsoAccount(acc.account_id)}
                                >
                                  {acc.account_name}
                                  <span className="tool-sso-option-sub">{acc.account_id}</span>
                                </button>
                              ))}
                              <button className="btn" onClick={cancelSsoFlow}>Cancel</button>
                            </>
                          )}
                          {ssoFlow.status === 'selecting-role' && (
                            <>
                              <p className="tool-sso-hint">Select a role:</p>
                              {ssoFlow.roles.length === 0 && (
                                <p className="tool-sso-waiting">Loading roles…</p>
                              )}
                              {ssoFlow.roles.map((r) => (
                                <button
                                  key={r.role_name}
                                  className="btn tool-sso-option"
                                  onClick={() => selectSsoRole(r.role_name)}
                                >
                                  {r.role_name}
                                </button>
                              ))}
                              <button className="btn" onClick={cancelSsoFlow}>Cancel</button>
                            </>
                          )}
                        </div>
                      ) : (
                        <button
                          className="btn btn-primary"
                          onClick={startAwsSso}
                          disabled={initiating['aws-sso']}
                        >
                          {initiating['aws-sso']
                            ? 'Opening browser…'
                            : tool.authenticated
                            ? 'Re-authenticate'
                            : 'Sign in with AWS SSO'}
                        </button>
                      )}
                    </div>
                  )}
                </>
              ) : tool.type === 'key' ? (
                // ── Key-based tool ───────────────────────────────────────────
                tool.vars.length === 0 ? (
                  <p className="credentials-no-vars">No credentials required.</p>
                ) : (
                  tool.vars.map((varName) => (
                    <div key={varName} className="credentials-var">
                      <div className="credentials-var-label">
                        <code className="credentials-var-name">{varName}</code>
                        {tool.stored[varName] ? (
                          <span className="credentials-badge credentials-badge--stored">Stored ✓</span>
                        ) : (
                          <span className="credentials-badge credentials-badge--missing">Not stored</span>
                        )}
                      </div>
                      <div className="credentials-var-row">
                        <input
                          type="password"
                          className="credentials-input"
                          placeholder={tool.stored[varName] ? 'Enter new value to update…' : 'Enter value…'}
                          value={inputs[varName] ?? ''}
                          onChange={(e) => setInputs((prev) => ({ ...prev, [varName]: e.target.value }))}
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
                )
              ) : (
                // ── No-auth tool ─────────────────────────────────────────────
                <p className="credentials-no-vars">No credentials required.</p>
              )}
            </div>
          </div>
        ))}
        <OutboundWebhooksPanel apiBase={apiBase} token={token} />
      </div>
    </div>
  )
}
