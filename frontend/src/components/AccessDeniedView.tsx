import { useState } from 'react'

interface Props {
  isAuthenticated: boolean
  onSignIn: () => void
  onRequestAccess: () => Promise<void>
}

export default function AccessDeniedView({ isAuthenticated, onSignIn, onRequestAccess }: Props) {
  const [requested, setRequested] = useState(false)
  const [requesting, setRequesting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleRequestAccess() {
    setRequesting(true)
    setError(null)
    try {
      await onRequestAccess()
      setRequested(true)
    } catch {
      setError('Failed to send request. Please try again.')
    } finally {
      setRequesting(false)
    }
  }

  return (
    <div className="auth-wall">
      <div className="auth-wall-content">
        <h2 style={{ marginBottom: '0.5rem', fontSize: '1.25rem', fontWeight: 600 }}>
          Access Denied
        </h2>
        <p style={{ marginBottom: '1.25rem', color: 'var(--text-muted)', fontSize: '0.9rem' }}>
          This page is private.
        </p>

        {!isAuthenticated ? (
          <>
            <p style={{ marginBottom: '1rem', fontSize: '0.875rem' }}>
              Sign in to request access from the page owner.
            </p>
            <button className="btn btn-primary auth-wall-btn" onClick={onSignIn}>
              Sign in with Google
            </button>
          </>
        ) : requested ? (
          <p style={{ color: 'var(--success, #38a169)', fontSize: '0.875rem' }}>
            Your access request has been sent to the page owner.
          </p>
        ) : (
          <>
            {error && (
              <p style={{ color: 'var(--danger, #e53e3e)', marginBottom: '0.75rem', fontSize: '0.875rem' }}>
                {error}
              </p>
            )}
            <button
              className="btn btn-primary auth-wall-btn"
              onClick={handleRequestAccess}
              disabled={requesting}
            >
              {requesting ? 'Sending…' : 'Request Access'}
            </button>
          </>
        )}
      </div>
    </div>
  )
}
