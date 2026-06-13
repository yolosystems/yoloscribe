import { useState, useRef, useEffect, useCallback, KeyboardEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ChevronDown, ChevronRight, ChevronsLeft, ChevronsRight } from 'lucide-react'
import AgentsList from './AgentsList'
import VersionsPanel from './VersionsPanel'
import type { AgentMeta } from '../App'

export interface VersionMeta {
  version_id: string
  last_modified: string
}

interface Message {
  role: 'user' | 'assistant'
  content: string
  thinking?: boolean
  proposedContent?: string
}

interface TokenBudget {
  used: number
  limit: number
  resets_at: string
}

interface Props {
  content: string
  onContentUpdate: (newContent: string) => void
  onApplyProposedContent?: (newContent: string) => void
  apiBase: string
  site: string
  filePath: string
  token: string
  showAgents?: boolean
  agents?: AgentMeta[]
  activeFilePath?: string
  pagePath?: string
  onAgentsChanged?: () => void
  showVersions?: boolean
  selectedVersionId?: string | null
  onVersionSelect?: (version: VersionMeta | null) => void
}

const MIN_WIDTH = 220
const MAX_WIDTH = 600
const DEFAULT_WIDTH = 340

export default function ChatPanel({
  content, onContentUpdate, onApplyProposedContent,
  apiBase, site, filePath, token,
  showAgents = true, agents = [], activeFilePath = '', pagePath = '', onAgentsChanged,
  showVersions = false, selectedVersionId = null, onVersionSelect,
}: Props) {
  const [messages, setMessages] = useState<Message[]>([{
    role: 'assistant',
    content: "Hi! I can help you edit this wiki page. Tell me what changes you'd like to make.",
  }])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [agentsOpen, setAgentsOpen] = useState(true)
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [expanded, setExpanded] = useState(true)
  const [width, setWidth] = useState(DEFAULT_WIDTH)
  const [tokenBudget, setTokenBudget] = useState<TokenBudget | null>(null)
  const sessionId = useRef<string>(crypto.randomUUID())
  const bottomRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    if (!token) return
    fetch(`${apiBase}/token-budget`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => { if (data) setTokenBudget(data) })
      .catch(() => {})
  }, [apiBase, token])

  const onMouseMove = useCallback((e: MouseEvent) => {
    if (!dragRef.current) return
    const delta = e.clientX - dragRef.current.startX
    setWidth(Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, dragRef.current.startWidth + delta)))
  }, [])

  const onMouseUp = useCallback(() => {
    dragRef.current = null
    document.removeEventListener('mousemove', onMouseMove)
    document.removeEventListener('mouseup', onMouseUp)
  }, [onMouseMove])

  function onResizeMouseDown(e: React.MouseEvent) {
    e.preventDefault()
    dragRef.current = { startX: e.clientX, startWidth: width }
    document.addEventListener('mousemove', onMouseMove)
    document.addEventListener('mouseup', onMouseUp)
  }

  async function send() {
    const text = input.trim()
    if (!text || loading) return

    setMessages((prev) => [...prev, { role: 'user', content: text }])
    setInput('')
    setLoading(true)
    setMessages((prev) => [...prev, { role: 'assistant', content: 'Thinking…', thinking: true }])

    try {
      const res = await fetch(`${apiBase}/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token && { Authorization: `Bearer ${token}` }),
        },
        body: JSON.stringify({
          message: text,
          current_content: content,
          history: messages.map((m) => ({ role: m.role, content: m.content })),
          site,
          file_path: filePath,
          session_id: sessionId.current,
        }),
      })
      if (!res.ok) {
        if (res.status === 429) {
          const body = await res.json().catch(() => ({}))
          throw new Error(body.detail ?? 'Daily token budget exhausted. Try again tomorrow.')
        }
        throw new Error(`Server error: ${res.status}`)
      }
      const data = await res.json()

      if (data.token_budget) setTokenBudget(data.token_budget)

      setMessages((prev) => {
        const without = prev.filter((m) => !m.thinking)
        const reply: Message = { role: 'assistant', content: data.reply }
        if (data.updated_content != null && data.updated_content !== content) {
          if (onApplyProposedContent) {
            reply.proposedContent = data.updated_content
          } else {
            onContentUpdate(data.updated_content)
          }
        }
        return [...without, reply]
      })

      if (data.navigate_to) {
        window.location.hash = data.navigate_to
      }
    } catch (err) {
      setMessages((prev) => {
        const without = prev.filter((m) => !m.thinking)
        return [...without, {
          role: 'assistant',
          content: `Error: ${err instanceof Error ? err.message : 'Unknown error'}`,
        }]
      })
    } finally {
      setLoading(false)
    }
  }

  function applyProposed(proposedContent: string) {
    onApplyProposedContent!(proposedContent)
    setMessages((prev) => prev.map((m) =>
      m.proposedContent === proposedContent ? { ...m, proposedContent: undefined } : m
    ))
  }

  function cancelProposed(proposedContent: string) {
    setMessages((prev) => prev.map((m) =>
      m.proposedContent === proposedContent ? { ...m, proposedContent: undefined } : m
    ))
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      send()
    }
  }

  if (!expanded) {
    return (
      <div className="chat-panel chat-panel-collapsed" onClick={() => setExpanded(true)} title="Open chat">
        <ChevronsRight size={16} style={{ color: 'var(--text-muted)' }} />
        <span className="chat-panel-collapsed-label">Chat</span>
      </div>
    )
  }

  return (
    <div className="chat-panel" style={{ width }}>
      <div className="chat-panel-resize-handle" onMouseDown={onResizeMouseDown} />

      <div className="chat-panel-header">
        <span>Chat</span>
        <button className="btn btn-icon" title="Collapse chat" onClick={() => setExpanded(false)}>
          <ChevronsLeft size={14} />
        </button>
      </div>

      {showAgents && (
        <div className="chat-agents-accordion">
          <button
            className="chat-agents-accordion-header"
            onClick={() => setAgentsOpen((o) => !o)}
          >
            {agentsOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            <span>Agents{agents.length > 0 ? ` (${agents.length})` : ''}</span>
          </button>
          {agentsOpen && (
            <div className="chat-agents-list">
              <AgentsList
                agents={agents}
                activeFilePath={activeFilePath}
                pagePath={pagePath}
                apiBase={apiBase}
                site={site}
                token={token}
                onAgentsChanged={onAgentsChanged ?? (() => {})}
                embedded
              />
            </div>
          )}
        </div>
      )}

      {showVersions && (
        <div className="chat-agents-accordion">
          <button
            className="chat-agents-accordion-header"
            onClick={() => setVersionsOpen((o) => !o)}
          >
            {versionsOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            <span>Versions</span>
          </button>
          {versionsOpen && (
            <div className="chat-agents-list">
              <VersionsPanel
                apiBase={apiBase}
                site={site}
                pagePath={pagePath}
                token={token}
                selectedVersionId={selectedVersionId}
                onVersionSelect={onVersionSelect ?? (() => {})}
              />
            </div>
          )}
        </div>
      )}

      <div className="chat-messages">
        {messages.map((msg, i) => (
          <div key={i} className={`chat-message ${msg.role}${msg.thinking ? ' thinking' : ''}`}>
            {msg.role === 'assistant' && !msg.thinking ? (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
            ) : (
              msg.content
            )}
            {msg.proposedContent != null && (
              <div className="chat-confirm-card">
                <div className="chat-confirm-title">✏️ Ready to edit this page</div>
                <div className="chat-confirm-actions">
                  <button className="btn btn-primary" onClick={() => applyProposed(msg.proposedContent!)}>
                    Apply changes
                  </button>
                  <button className="btn" onClick={() => cancelProposed(msg.proposedContent!)}>
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <div className="chat-input-area">
        <textarea
          className="chat-input"
          placeholder="Describe the changes you want… (⌘+Enter to send)"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={loading}
        />
        <button className="btn btn-primary" onClick={send} disabled={loading}>
          {loading ? 'Sending…' : 'Send'}
        </button>
        {tokenBudget && (
          <div className="chat-token-budget">
            {tokenBudget.used.toLocaleString()} / {tokenBudget.limit.toLocaleString()} tokens today
          </div>
        )}
      </div>
    </div>
  )
}
