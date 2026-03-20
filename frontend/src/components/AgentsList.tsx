import { useState } from 'react'
import CreateAgentModal from './CreateAgentModal'

interface Props {
  agents: string[]
  activeFilePath: string
  pagePath: string
  apiBase: string
  site: string
  token: string
  onAgentsChanged: () => void
}

export default function AgentsList({ agents, activeFilePath, pagePath, apiBase, site, token, onAgentsChanged }: Props) {
  const [showCreate, setShowCreate] = useState(false)

  function navigate(name: string) {
    const hash = pagePath ? `#/${pagePath}/.agents/${name}` : `#/.agents/${name}`
    window.location.hash = hash
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
    await fetch(`${apiBase}/agents?${params}`, {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${token}` },
    })
    onAgentsChanged()
  }

  function handleCreateSuccess(agentName: string) {
    setShowCreate(false)
    onAgentsChanged()
    navigate(agentName)
  }

  return (
    <>
      <div className="agents-panel">
        <div className="agents-panel-header">
          <span>Agents</span>
          <button
            className="agents-add-btn"
            title="Create new agent"
            onClick={() => setShowCreate(true)}
          >
            +
          </button>
        </div>
        <div className="agents-list">
          {agents.length === 0 ? (
            <div className="agents-empty">No agents</div>
          ) : (
            agents.map((name) => (
              <div key={name} className={`agents-item-row${isActive(name) ? ' active' : ''}`}>
                <button
                  className="agents-item-name"
                  onClick={() => navigate(name)}
                >
                  {name}
                </button>
                <button
                  className="agents-delete-btn"
                  title={`Delete ${name}`}
                  onClick={(e) => handleDelete(name, e)}
                >
                  🗑
                </button>
              </div>
            ))
          )}
        </div>
      </div>

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
