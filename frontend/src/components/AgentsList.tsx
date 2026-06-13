import { useState, useEffect } from 'react'
import CreateAgentModal from './CreateAgentModal'
import type { AgentMeta } from '../App'

interface Props {
  agents: AgentMeta[]
  activeFilePath: string
  pagePath: string
  apiBase: string
  site: string
  token: string
  onAgentsChanged: () => void
  embedded?: boolean
}

const TRIGGER_LABELS: Record<string, string> = {
  manual: 'manual',
  schedule: 'schedule',
  on_write: 'on write',
}

function AgentRunsList({ agentName, pagePath, apiBase, site, token }: {
  agentName: string
  pagePath: string
  apiBase: string
  site: string
  token: string
}) {
  const [runs, setRuns] = useState<string[] | null>(null)

  useEffect(() => {
    const params = new URLSearchParams({ site, agent_name: agentName })
    if (pagePath) params.set('page_path', pagePath)
    fetch(`${apiBase}/agent-runs?${params}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(r => r.json())
      .then(d => setRuns(d.runs ?? []))
      .catch(() => setRuns([]))
  }, [agentName, pagePath, apiBase, site, token])

  function navigateRun(filename: string) {
    const path = pagePath
      ? `${pagePath}/.agents/${agentName}/runs/${filename}`
      : `.agents/${agentName}/runs/${filename}`
    window.location.hash = `#/${path}`
  }

  if (runs === null) return <div className="agents-runs-loading">loading…</div>
  if (runs.length === 0) return <div className="agents-runs-empty">No runs yet</div>

  return (
    <ul className="agents-runs-list">
      {runs.map(filename => {
        const label = filename.replace(/\.md$/, '')
        return (
          <li key={filename} className="agents-run-item" onClick={() => navigateRun(filename)}>
            {label}
          </li>
        )
      })}
    </ul>
  )
}

export default function AgentsList({ agents, activeFilePath, pagePath, apiBase, site, token, onAgentsChanged, embedded = false }: Props) {
  const [showCreate, setShowCreate] = useState(false)
  const [expandedRuns, setExpandedRuns] = useState<Set<string>>(new Set())

  function navigate(name: string) {
    const hash = pagePath ? `#/${pagePath}/.agents/${name}` : `#/.agents/${name}`
    window.location.hash = hash
  }

  function navigateRunLog(name: string) {
    const path = pagePath
      ? `${pagePath}/.agents/${name}/run_log.md`
      : `.agents/${name}/run_log.md`
    window.location.hash = `#/${path}`
  }

  function isActive(name: string) {
    const expected = pagePath
      ? `${pagePath}/.agents/${name}/agent.md`
      : `.agents/${name}/agent.md`
    return activeFilePath === expected
  }

  function toggleRuns(name: string, e: React.MouseEvent) {
    e.stopPropagation()
    setExpandedRuns(prev => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  async function handleDelete(name: string, e: React.MouseEvent) {
    e.stopPropagation()
    if (!window.confirm(`Delete agent "${name}"? This cannot be undone.`)) return
    const params = new URLSearchParams({ site, agent_name: name })
    if (pagePath) params.set('page_path', pagePath)
    const res = await fetch(`${apiBase}/agents?${params}`, {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${token}` },
    })
    if (!res.ok) {
      const body = await res.json().catch(() => ({}))
      alert(`Failed to delete agent: ${body.detail ?? res.status}`)
      return
    }
    onAgentsChanged()
  }

  function handleCreateSuccess(agentName: string) {
    setShowCreate(false)
    onAgentsChanged()
    navigate(agentName)
  }

  const list = (
    <div className="agents-list">
      {agents.length === 0 ? (
        <div className="agents-empty">No agents</div>
      ) : (
        agents.map((agent) => (
          <div key={agent.name} className={`agents-item-wrap${isActive(agent.name) ? ' active' : ''}`}>
            <div className="agents-item-row">
              <div className="agents-item-main" onClick={() => navigate(agent.name)}>
                <span className="agents-item-name">{agent.name}</span>
                <span className={`agents-trigger-badge agents-trigger-${agent.trigger.replace('_', '-')}`}>
                  {agent.is_pointer ? 'ref' : (TRIGGER_LABELS[agent.trigger] ?? agent.trigger)}
                </span>
                {agent.scope.length > 0 && (
                  <span className="agents-scope-hint" title={agent.scope.join(', ')}>
                    {agent.scope[0]}{agent.scope.length > 1 ? ` +${agent.scope.length - 1}` : ''}
                  </span>
                )}
              </div>
              <div className="agents-item-actions">
                {agent.eval_log && (
                  <button
                    className={`agents-runs-btn${expandedRuns.has(agent.name) ? ' active' : ''}`}
                    title="View annotation runs"
                    onClick={(e) => toggleRuns(agent.name, e)}
                  >
                    runs
                  </button>
                )}
                <button
                  className="agents-log-btn"
                  title="View run log"
                  onClick={(e) => { e.stopPropagation(); navigateRunLog(agent.name) }}
                >
                  log
                </button>
                <button
                  className="agents-delete-btn"
                  title={`Delete ${agent.name}`}
                  onClick={(e) => handleDelete(agent.name, e)}
                >
                  🗑
                </button>
              </div>
            </div>
            {agent.eval_log && expandedRuns.has(agent.name) && (
              <div className="agents-runs-panel">
                <AgentRunsList
                  agentName={agent.name}
                  pagePath={pagePath}
                  apiBase={apiBase}
                  site={site}
                  token={token}
                />
              </div>
            )}
          </div>
        ))
      )}
    </div>
  )

  return (
    <>
      {embedded ? list : (
        <div className="agents-panel">
          <div className="agents-panel-header">
            <span>Agents</span>
            <button className="agents-add-btn" title="Create new agent" onClick={() => setShowCreate(true)}>
              +
            </button>
          </div>
          {list}
        </div>
      )}

      {showCreate && (
        <CreateAgentModal
          apiBase={apiBase}
          site={site}
          token={token}
          pagePath={pagePath}
          onSuccess={handleCreateSuccess}
          onClose={() => setShowCreate(false)}
        />
      )}
    </>
  )
}
