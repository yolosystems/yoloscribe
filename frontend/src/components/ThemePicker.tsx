interface Props {
  value: string
  onChange: (theme: string) => void
}

const THEMES = [
  {
    id: 'light',
    label: 'Light',
    colors: { bg: '#ffffff', surface: '#f6f8fa', border: '#d0d7de', text: '#1f2328', accent: '#0969da' },
  },
  {
    id: 'dark',
    label: 'Dark',
    colors: { bg: '#0d1117', surface: '#161b22', border: '#30363d', text: '#e6edf3', accent: '#58a6ff' },
  },
  {
    id: 'yolo',
    label: 'Yolo',
    colors: { bg: '#05050f', surface: '#0c0c1e', border: '#2a1f6e', text: '#f5f0ff', accent: '#ff6f3c' },
  },
]

export default function ThemePicker({ value, onChange }: Props) {
  return (
    <div className="theme-picker">
      {THEMES.map((theme) => (
        <button
          key={theme.id}
          type="button"
          className={`theme-card${value === theme.id ? ' theme-card--selected' : ''}`}
          onClick={() => onChange(theme.id)}
        >
          <div
            className="theme-preview"
            style={{ background: theme.colors.bg, border: `1px solid ${theme.colors.border}` }}
          >
            <div
              className="theme-preview-bar"
              style={{ background: theme.colors.surface, borderBottom: `1px solid ${theme.colors.border}` }}
            >
              <span style={{ color: theme.colors.text, fontSize: '6px', fontWeight: 600 }}>
                YoloScribe
              </span>
            </div>
            <div className="theme-preview-content">
              <div style={{ height: 4, background: theme.colors.text, opacity: 0.7, borderRadius: 2, width: '60%', marginBottom: 3 }} />
              <div style={{ height: 3, background: theme.colors.text, opacity: 0.4, borderRadius: 2, width: '85%', marginBottom: 2 }} />
              <div style={{ height: 3, background: theme.colors.text, opacity: 0.4, borderRadius: 2, width: '70%', marginBottom: 4 }} />
              <div style={{ height: 3, background: theme.colors.accent, opacity: 0.8, borderRadius: 2, width: '40%' }} />
            </div>
          </div>
          <span className="theme-label">{theme.label}</span>
        </button>
      ))}
    </div>
  )
}
