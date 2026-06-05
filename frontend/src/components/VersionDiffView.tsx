import { useState, useEffect } from 'react'
import { X } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface Version {
  version_id: string
  last_modified: string
}

interface Props {
  apiBase: string
  site: string
  pagePath: string
  token: string
  version: Version
  currentContent: string
  onClose: () => void
  onRestored: (newContent: string) => void
}

function formatTimestamp(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

export default function VersionDiffView({
  apiBase, site, pagePath, token, version, currentContent, onClose, onRestored,
}: Props) {
  const [versionContent, setVersionContent] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [restoring, setRestoring] = useState(false)

  useEffect(() => {
    setLoading(true)
    setError(null)
    fetch(
      `${apiBase}/version?site=${encodeURIComponent(site)}&page_path=${encodeURIComponent(pagePath)}&version_id=${encodeURIComponent(version.version_id)}`,
      { headers: { Authorization: `Bearer ${token}` } },
    )
      .then((res) => (res.ok ? res.text() : Promise.reject(new Error(`${res.status}`))))
      .then((text) => setVersionContent(text))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [apiBase, site, pagePath, token, version.version_id])

  async function restore() {
    setRestoring(true)
    try {
      const res = await fetch(
        `${apiBase}/restore?site=${encodeURIComponent(site)}&page_path=${encodeURIComponent(pagePath)}&version_id=${encodeURIComponent(version.version_id)}`,
        { method: 'POST', headers: { Authorization: `Bearer ${token}` } },
      )
      if (!res.ok) throw new Error(`${res.status}`)
      onRestored(versionContent!)
    } catch (err) {
      alert(`Restore failed: ${err instanceof Error ? err.message : 'Unknown error'}`)
    } finally {
      setRestoring(false)
    }
  }

  return (
    <div className="version-diff-view">
      <div className="version-diff-toolbar">
        <span className="version-diff-title">
          Comparing version from <strong>{formatTimestamp(version.last_modified)}</strong> with current
        </span>
        <div className="version-diff-actions">
          {versionContent !== null && versionContent !== currentContent && (
            <button className="btn btn-primary" onClick={restore} disabled={restoring}>
              {restoring ? 'Restoring…' : 'Restore this version'}
            </button>
          )}
          {versionContent !== null && versionContent === currentContent && (
            <span style={{ fontSize: '0.85em', color: 'var(--text-muted)' }}>No changes</span>
          )}
          <button className="btn btn-icon" onClick={onClose} title="Close diff view">
            <X size={16} />
          </button>
        </div>
      </div>

      {loading && <div className="state-center" style={{ padding: '2rem' }}>Loading version…</div>}
      {error && <div className="state-center" style={{ padding: '2rem' }}>Failed to load version</div>}

      {!loading && !error && versionContent !== null && (
        <div className="version-diff-panes">
          <div className="version-diff-pane">
            <div className="version-diff-pane-header">
              {formatTimestamp(version.last_modified)}
            </div>
            <div className="version-diff-pane-content">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{versionContent}</ReactMarkdown>
            </div>
          </div>
          <div className="version-diff-pane">
            <div className="version-diff-pane-header">Current</div>
            <div className="version-diff-pane-content">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{currentContent}</ReactMarkdown>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
