import { useState } from 'react'
import { Check, X } from 'lucide-react'
import MarkdownViewer from './MarkdownViewer'

interface Props {
  proposedContent: string
  currentContent: string
  apiBase: string
  site: string
  pagePath: string
  token: string
  onAccepted: (newContent: string) => void
  onRejected: () => void
}

export default function ProposedChangeBanner({
  proposedContent,
  currentContent,
  apiBase,
  site,
  pagePath,
  token,
  onAccepted,
  onRejected,
}: Props) {
  const [reviewOpen, setReviewOpen] = useState(false)
  const [acting, setActing] = useState(false)
  const [view, setView] = useState<'proposed' | 'current'>('proposed')

  async function accept() {
    setActing(true)
    try {
      const res = await fetch(
        `${apiBase}/accept-proposed?site=${encodeURIComponent(site)}&page_path=${encodeURIComponent(pagePath)}`,
        { method: 'POST', headers: { Authorization: `Bearer ${token}` } },
      )
      if (!res.ok) throw new Error(`Accept failed: ${res.status}`)
      onAccepted(proposedContent)
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Accept failed')
    } finally {
      setActing(false)
    }
  }

  async function reject() {
    setActing(true)
    try {
      const res = await fetch(
        `${apiBase}/reject-proposed?site=${encodeURIComponent(site)}&page_path=${encodeURIComponent(pagePath)}`,
        { method: 'POST', headers: { Authorization: `Bearer ${token}` } },
      )
      if (!res.ok) throw new Error(`Reject failed: ${res.status}`)
      onRejected()
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Reject failed')
    } finally {
      setActing(false)
    }
  }

  return (
    <>
      <div
        style={{
          background: 'var(--surface)',
          borderBottom: '2px solid var(--accent, #f59e0b)',
          padding: '8px 16px',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          fontSize: '0.9em',
        }}
      >
        <span style={{ flex: 1 }}>
          An agent has proposed changes to this page.
        </span>
        <button
          className="btn"
          onClick={() => setReviewOpen((o) => !o)}
        >
          {reviewOpen ? 'Hide' : 'Review'}
        </button>
        <button
          className="btn btn-primary"
          onClick={accept}
          disabled={acting}
          title="Accept proposed changes"
        >
          <Check size={14} style={{ marginRight: 4 }} />
          Accept
        </button>
        <button
          className="btn btn-danger"
          onClick={reject}
          disabled={acting}
          title="Reject proposed changes"
        >
          <X size={14} style={{ marginRight: 4 }} />
          Reject
        </button>
      </div>

      {reviewOpen && (
        <div style={{ borderBottom: '1px solid var(--border, #e2e8f0)' }}>
          <div style={{ display: 'flex', gap: 8, padding: '8px 16px', background: 'var(--bg)' }}>
            <button
              className={`btn${view === 'proposed' ? ' btn-primary' : ''}`}
              style={{ fontSize: '0.85em' }}
              onClick={() => setView('proposed')}
            >
              Proposed
            </button>
            <button
              className={`btn${view === 'current' ? ' btn-primary' : ''}`}
              style={{ fontSize: '0.85em' }}
              onClick={() => setView('current')}
            >
              Current
            </button>
          </div>
          <div style={{ padding: '0 16px 16px', maxHeight: '50vh', overflowY: 'auto' }}>
            <MarkdownViewer
              content={view === 'proposed' ? proposedContent : currentContent}
              site={site}
              apiBase={apiBase}
              pagePath={pagePath}
            />
          </div>
        </div>
      )}
    </>
  )
}
