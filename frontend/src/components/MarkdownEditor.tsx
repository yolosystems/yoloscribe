import MDEditor from '@uiw/react-md-editor'

interface Props {
  content: string
  onChange: (value: string) => void
}

export default function MarkdownEditor({ content, onChange }: Props) {
  return (
    <div className="markdown-editor-wrapper" data-color-mode="dark">
      <MDEditor
        value={content}
        onChange={(value) => onChange(value ?? '')}
        height="100%"
        preview="live"
        visibleDragbar={false}
      />
    </div>
  )
}
