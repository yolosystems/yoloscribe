import { useState, useEffect } from 'react'
import { Trash2 } from 'lucide-react'

interface MessagingConfig {
  id: string
  platform: string
  connection: Record<string, string>
  created_at: string
  api_token_id: string
  api_token_name: string
}

interface Props {
  apiBase: string
  token: string
  site: string
}

const PLATFORM_LABELS: Record<string, string> = {
  discord: 'Discord',
  slack: 'Slack',
  telegram: 'Telegram',
}

function platformLabel(platform: string): string {
  return PLATFORM_LABELS[platform] ?? platform
}

function channelSummary(platform: string, connection: Record<string, string>): string {
  if (platform === 'discord') {
    const parts = []
    if (connection.guild_id) parts.push(`guild ${connection.guild_id}`)
    if (connection.channel_id) parts.push(`#${connection.channel_id}`)
    return parts.join(' / ')
  }
  if (platform === 'slack') {
    const parts = []
    if (connection.workspace_id) parts.push(`workspace ${connection.workspace_id}`)
    if (connection.channel_id) parts.push(`#${connection.channel_id}`)
    return parts.join(' / ')
  }
  if (platform === 'telegram') {
    return connection.chat_id ? `chat ${connection.chat_id}` : ''
  }
  return Object.entries(connection).map(([k, v]) => `${k}: ${v}`).join(', ')
}

export default function MessagingConnectionsPanel({ apiBase, token, site }: Props) {
  const [configs, setConfigs] = useState<MessagingConfig[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<Record<string, boolean>>({})

  useEffect(() => { load() }, [apiBase, site])

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${apiBase}/messaging-configs?site=${encodeURIComponent(site)}`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setConfigs(data.configs)
    } catch (e) {
      setError(`Failed to load messaging connections: ${e instanceof Error ? e.message : e}`)
    } finally {
      setLoading(false)
    }
  }

  async function deleteConfig(id: string) {
    setDeleting((prev) => ({ ...prev, [id]: true }))
    try {
      const res = await fetch(
        `${apiBase}/messaging-config?site=${encodeURIComponent(site)}&config_id=${encodeURIComponent(id)}`,
        { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } },
      )
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setConfigs((prev) => prev.filter((c) => c.id !== id))
    } catch (e) {
      alert(`Failed to remove connection: ${e instanceof Error ? e.message : e}`)
    } finally {
      setDeleting((prev) => ({ ...prev, [id]: false }))
    }
  }

  return (
    <div className="credentials-section">
      <div className="credentials-section-header">Messaging Connections</div>
      <p className="credentials-section-description">
        Channels connected to this site via the YoloScribe bot. Run <code>/yoloscribe setup &lt;api_token&gt;</code> in Discord to add a channel.
      </p>

      {error && <div className="credentials-banner credentials-banner--error">{error}</div>}

      {loading ? (
        <div className="credentials-empty">Loading…</div>
      ) : configs.length === 0 ? (
        <p className="credentials-empty">No channels connected.</p>
      ) : (
        <div className="credentials-webhook-list">
          {configs.map((c) => (
            <div key={c.id} className="credentials-webhook-row">
              <div className="credentials-webhook-info">
                <span className="credentials-webhook-label">{platformLabel(c.platform)}</span>
                <code className="credentials-webhook-url">{channelSummary(c.platform, c.connection)}</code>
                <span className="credentials-oauth-expiry">token: {c.api_token_name}</span>
              </div>
              <button
                className="btn btn-danger btn-icon"
                onClick={() => deleteConfig(c.id)}
                disabled={deleting[c.id]}
                title="Disconnect channel"
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
