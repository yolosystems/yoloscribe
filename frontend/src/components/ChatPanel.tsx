import { useState, useRef, useEffect, KeyboardEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface Message {
  role: 'user' | 'assistant'
  content: string
  thinking?: boolean
}

interface Props {
  content: string
  onContentUpdate: (newContent: string) => void
  apiBase: string
  site: string
  filePath: string
  token: string
}

export default function ChatPanel({ content, onContentUpdate, apiBase, site, filePath, token }: Props) {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: 'assistant',
      content: 'Hi! I can help you edit this wiki page. Tell me what changes you\'d like to make.',
    },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function send() {
    const text = input.trim()
    if (!text || loading) return

    const userMsg: Message = { role: 'user', content: text }
    setMessages((prev) => [...prev, userMsg])
    setInput('')
    setLoading(true)

    const thinkingMsg: Message = { role: 'assistant', content: 'Thinking…', thinking: true }
    setMessages((prev) => [...prev, thinkingMsg])

    try {
      const res = await fetch(`${apiBase}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(token && { Authorization: `Bearer ${token}` }) },
        body: JSON.stringify({
          message: text,
          current_content: content,
          history: messages.map((m) => ({ role: m.role, content: m.content })),
          site,
          file_path: filePath,
        }),
      })

      if (!res.ok) throw new Error(`Server error: ${res.status}`)

      const data = await res.json()

      setMessages((prev) => {
        const without = prev.filter((m) => !m.thinking)
        return [...without, { role: 'assistant', content: data.reply }]
      })

      if (data.updated_content != null && data.updated_content !== content) {
        onContentUpdate(data.updated_content)
      }
    } catch (err) {
      setMessages((prev) => {
        const without = prev.filter((m) => !m.thinking)
        return [
          ...without,
          { role: 'assistant', content: `Error: ${err instanceof Error ? err.message : 'Unknown error'}` },
        ]
      })
    } finally {
      setLoading(false)
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className="chat-panel">
      <div className="chat-panel-header">Chat</div>

      <div className="chat-messages">
        {messages.map((msg, i) => (
          <div
            key={i}
            className={`chat-message ${msg.role}${msg.thinking ? ' thinking' : ''}`}
          >
            {msg.role === 'assistant' && !msg.thinking ? (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
            ) : (
              msg.content
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
      </div>
    </div>
  )
}
