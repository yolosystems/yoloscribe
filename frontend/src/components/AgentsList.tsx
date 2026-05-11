import { useState } from 'react'
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

export default function AgentsList({ agents, activeFilePath, pagePath, apiBase, site, token, onAgentsChanged, embedded = false }: Props) {
  const [showCreate, setShowCreate] = useState(false)

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
          <div key={agent.name} className={`agents-item-row${isActive(agent.name) ? ' active' : ''}`}>
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
