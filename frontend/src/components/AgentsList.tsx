interface Props {
  agents: string[]
  activeFilePath: string
}

export default function AgentsList({ agents, activeFilePath }: Props) {
  function navigate(name: string) {
    window.location.hash = `#/.agents/${name}`
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
              className={`agents-item${activeFilePath === `.agents/${name}/agent.md` ? ' active' : ''}`}
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
