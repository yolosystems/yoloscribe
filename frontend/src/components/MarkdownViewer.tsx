import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { resolveAssetUrl } from '../assets'

interface Props {
  content: string
  site: string
  apiBase: string
}

const VIDEO_RE = /\.(mp4|m4v)$/i
const AUDIO_RE = /\.m4a$/i

export default function MarkdownViewer({ content, site, apiBase }: Props) {
  return (
    <div className="markdown-viewer">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          img({ src, alt }) {
            if (!src) return null
            const url = resolveAssetUrl(src, site, apiBase)
            if (VIDEO_RE.test(src)) {
              return (
                <video
                  src={url}
                  controls
                  preload="metadata"
                  style={{ maxWidth: '100%' }}
                  title={alt ?? undefined}
                />
              )
            }
            if (AUDIO_RE.test(src)) {
              return (
                <audio
                  src={url}
                  controls
                  title={alt ?? undefined}
                />
              )
            }
            return (
              <img
                src={url}
                alt={alt ?? ''}
                style={{ maxWidth: '100%' }}
              />
            )
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
}
