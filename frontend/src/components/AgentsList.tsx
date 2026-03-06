interface Props {
  agents: string[]
  activeFilePath: string
  pagePath: string
}

export default function AgentsList({ agents, activeFilePath, pagePath }: Props) {
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

  return (
    <div className="agents-panel">
      <div className="agents-panel-header">Agents</div>
      <div className="agents-list">
        {agents.length === 0 ? (
          <div className="agents-empty">No agents</div>
        ) : (
          agents.map((name) => (
            <button
              key={name}
              className={`agents-item${isActive(name) ? ' active' : ''}`}
              onClick={() => navigate(name)}
            >
              {name}
            </button>
          ))
        )}
      </div>
    </div>
  )
}
