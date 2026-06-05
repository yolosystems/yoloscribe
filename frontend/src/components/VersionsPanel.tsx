import { useState, useEffect } from 'react'

interface Version {
  version_id: string
  last_modified: string
  size_bytes: number
  is_latest: boolean
}

interface Props {
  apiBase: string
  site: string
  pagePath: string
  token: string
  selectedVersionId: string | null
  onVersionSelect: (version: Version | null) => void
}

function formatTimestamp(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  })
}

export default function VersionsPanel({ apiBase, site, pagePath, token, selectedVersionId, onVersionSelect }: Props) {
  const [versions, setVersions] = useState<Version[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    fetch(
      `${apiBase}/versions?site=${encodeURIComponent(site)}&page_path=${encodeURIComponent(pagePath)}`,
      { headers: { Authorization: `Bearer ${token}` } },
    )
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`${res.status}`))))
      .then((data) => setVersions(data.versions ?? []))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }, [apiBase, site, pagePath, token])

  if (loading) return <div className="versions-panel-empty">Loading…</div>
  if (error) return <div className="versions-panel-empty">Failed to load versions</div>
  if (versions.length === 0) return <div className="versions-panel-empty">No version history yet</div>

  return (
    <div className="versions-list">
      {versions.map((v) => (
        <button
          key={v.version_id}
          className={`versions-item${selectedVersionId === v.version_id ? ' versions-item-selected' : ''}`}
          onClick={() => onVersionSelect(selectedVersionId === v.version_id ? null : v)}
          title={v.version_id}
        >
          <span className="versions-item-label">
            {v.is_latest ? 'Current' : formatTimestamp(v.last_modified)}
          </span>
          {v.is_latest && (
            <span className="versions-item-meta">{formatTimestamp(v.last_modified)}</span>
          )}
        </button>
      ))}
    </div>
  )
}
