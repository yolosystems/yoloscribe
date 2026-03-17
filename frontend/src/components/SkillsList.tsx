interface Props {
  skills: string[]
  activeFilePath: string
}

export default function SkillsList({ skills, activeFilePath }: Props) {
  function navigate(name: string) {
    window.location.hash = `#/.skills/${name}`
  }

  function isActive(name: string) {
    return activeFilePath === `.skills/${name}/SKILL.md`
  }

  return (
    <div className="agents-panel">
      <div className="agents-panel-header">Skills</div>
      <div className="agents-list">
        {skills.length === 0 ? (
          <div className="agents-empty">No skills</div>
        ) : (
          skills.map((name) => (
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
