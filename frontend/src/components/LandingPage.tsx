interface Props {
  onSignIn: () => void
}

export default function LandingPage({ onSignIn }: Props) {
  return (
    <>
      <header className="topbar">
        <span className="topbar-title">Yolo Scribe</span>
        <div className="topbar-actions">
          <button className="btn" onClick={onSignIn}>Sign in</button>
        </div>
      </header>
      <div className="landing-page">
        <div className="landing-content">
          <h1>Your AI-powered wiki</h1>
          <p className="landing-tagline">
            AgentScribe gives you a personal wiki where AI agents help you write,
            organise, and navigate your knowledge. Free to get started.
          </p>
          <button className="btn btn-primary landing-cta" onClick={onSignIn}>
            Create your Free Site
          </button>
        </div>
      </div>
    </>
  )
}
