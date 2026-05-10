import { useRef, useState } from 'react'
import MDEditor, { commands } from '@uiw/react-md-editor'

// Allowed MIME types for the file picker (mirrors backend ASSET_ALLOWED_EXTENSIONS).
const ACCEPTED_MIME = [
  'image/jpeg', 'image/png', 'image/gif', 'image/webp',
  'video/mp4',
  'audio/mp4',
].join(',')

interface Props {
  content: string
  onChange: (value: string) => void
  // Owner-only upload props — omit to hide the upload button.
  isOwner?: boolean
  site?: string
  apiBase?: string
  token?: string
  pagePath?: string  // current page path (e.g. "" for root, "intro" for /intro)
}

export default function MarkdownEditor({
  content,
  onChange,
  isOwner,
  site,
  apiBase,
  token,
  pagePath = '',
}: Props) {
  const fileInputRef = useRef<HTMLInputElement>(null)
  // Holds the MDEditor API so the async upload callback can insert at cursor.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const editorApiRef = useRef<any>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState(0)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [dragOver, setDragOver] = useState(false)

  const showUpload = isOwner && site && apiBase && token

  // ── Core upload logic ────────────────────────────────────────────────────

  async function handleFiles(files: FileList | File[]) {
    const file = Array.isArray(files) ? files[0] : files[0]
    if (!file || !showUpload) return

    setUploadError(null)
    setUploading(true)
    setUploadProgress(0)

    try {
      // Sanitise filename: lowercase, replace spaces with hyphens, strip anything
      // that isn't alphanumeric, dot, hyphen, or underscore.
      const safeName = file.name
        .toLowerCase()
        .replace(/\s+/g, '-')
        .replace(/[^a-z0-9._-]/g, '')

      const isMediaFile = file.type.startsWith('video/') || file.type.startsWith('audio/')
      const subdir = isMediaFile ? 'media' : 'assets'
      const assetPath = pagePath
        ? `${pagePath}/${subdir}/${safeName}`
        : `${subdir}/${safeName}`

      // 1. Get pre-signed PUT URL from the backend.
      const uploadRes = await fetch(
        `${apiBase}/upload?site=${encodeURIComponent(site!)}&path=${encodeURIComponent(assetPath)}`,
        { method: 'POST', headers: { Authorization: `Bearer ${token}` } },
      )
      if (!uploadRes.ok) {
        const body = await uploadRes.json().catch(() => ({}))
        throw new Error(body.detail ?? `Upload request failed: ${uploadRes.status}`)
      }
      const { upload_url, content_type } = await uploadRes.json()

      // 2. PUT the file bytes directly to S3 via XHR (supports upload progress).
      await new Promise<void>((resolve, reject) => {
        const xhr = new XMLHttpRequest()
        xhr.open('PUT', upload_url)
        xhr.setRequestHeader('Content-Type', content_type)
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) setUploadProgress(Math.round((e.loaded / e.total) * 100))
        }
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) resolve()
          else reject(new Error(`S3 upload failed: ${xhr.status}`))
        }
        xhr.onerror = () => reject(new Error('S3 upload network error'))
        xhr.send(file)
      })

      // 3. Insert markdown snippet at cursor (or append if no api reference).
      const snippet = `![${safeName}](${assetPath.replace(`${pagePath}/`, '')})`
      if (editorApiRef.current) {
        editorApiRef.current.replaceSelection(snippet)
      } else {
        onChange(content + '\n' + snippet)
      }
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setUploading(false)
      setUploadProgress(0)
    }
  }

  // ── Custom toolbar upload command ────────────────────────────────────────

  const uploadCommand: commands.ICommand = {
    name: 'upload-media',
    keyCommand: 'upload-media',
    buttonProps: { title: 'Upload image, video, or audio', 'aria-label': 'Upload media' },
    icon: <span style={{ fontSize: '1rem', lineHeight: 1 }}>📎</span>,
    execute(_state, api) {
      editorApiRef.current = api
      fileInputRef.current?.click()
    },
  }

  // ── Drag-and-drop handlers ───────────────────────────────────────────────

  function onDragOver(e: React.DragEvent) {
    if (!showUpload) return
    e.preventDefault()
    setDragOver(true)
  }

  function onDragLeave() {
    setDragOver(false)
  }

  function onDrop(e: React.DragEvent) {
    if (!showUpload) return
    e.preventDefault()
    setDragOver(false)
    const files = e.dataTransfer.files
    if (files.length > 0) handleFiles(files)
  }

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <div
      className="markdown-editor-wrapper"
      data-color-mode="dark"
      style={{ position: 'relative' }}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      {/* Hidden file input triggered by the toolbar button */}
      {showUpload && (
        <input
          ref={fileInputRef}
          type="file"
          accept={ACCEPTED_MIME}
          style={{ display: 'none' }}
          onChange={(e) => {
            if (e.target.files?.length) handleFiles(e.target.files)
            // Reset so the same file can be re-uploaded.
            e.target.value = ''
          }}
        />
      )}

      <MDEditor
        value={content}
        onChange={(value) => onChange(value ?? '')}
        height="100%"
        preview="live"
        visibleDragbar={false}
        extraCommands={showUpload ? [uploadCommand, commands.fullscreen] : [commands.fullscreen]}
      />

      {/* Drag-over overlay */}
      {dragOver && (
        <div style={{
          position: 'absolute', inset: 0,
          background: 'rgba(0,0,0,0.45)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          borderRadius: 6,
          pointerEvents: 'none',
          zIndex: 10,
        }}>
          <span style={{ color: '#fff', fontSize: '1.1rem', fontWeight: 600 }}>
            Drop to upload
          </span>
        </div>
      )}

      {/* Upload progress bar */}
      {uploading && (
        <div style={{
          position: 'absolute', bottom: 0, left: 0, right: 0,
          background: 'var(--surface)', padding: '0.5rem 0.75rem',
          borderTop: '1px solid var(--border)',
          zIndex: 10,
        }}>
          <div style={{ fontSize: '0.8rem', marginBottom: '0.35rem', color: 'var(--text-muted)' }}>
            Uploading… {uploadProgress}%
          </div>
          <div style={{ height: 4, background: 'var(--border)', borderRadius: 2 }}>
            <div style={{
              height: '100%',
              width: `${uploadProgress}%`,
              background: 'var(--success, #38a169)',
              borderRadius: 2,
              transition: 'width 0.1s',
            }} />
          </div>
        </div>
      )}

      {/* Upload error */}
      {uploadError && (
        <div style={{
          position: 'absolute', bottom: 0, left: 0, right: 0,
          background: 'var(--surface)', padding: '0.5rem 0.75rem',
          borderTop: '1px solid var(--danger, #e53e3e)',
          color: 'var(--danger, #e53e3e)',
          fontSize: '0.8rem',
          zIndex: 10,
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        }}>
          <span>{uploadError}</span>
          <button
            style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'inherit' }}
            onClick={() => setUploadError(null)}
          >
            ✕
          </button>
        </div>
      )}
    </div>
  )
}
