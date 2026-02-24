export interface BreadcrumbSegment {
  label: string
  /** filePath to navigate to when clicked. null = non-navigable structural label. */
  filePath: string | null
}

interface Props {
  segments: BreadcrumbSegment[]
  onNavigate: (filePath: string) => void
}

export default function Breadcrumb({ segments, onNavigate }: Props) {
  return (
    <nav className="breadcrumb-bar">
      {segments.map((seg, i) => {
        const isLast = i === segments.length - 1
        return (
          <span key={i} className="breadcrumb-item">
            {i > 0 && <span className="breadcrumb-sep">/</span>}
            {!isLast && seg.filePath !== null ? (
              <button className="breadcrumb-link" onClick={() => onNavigate(seg.filePath!)}>
                {seg.label}
              </button>
            ) : (
              <span className={isLast ? 'breadcrumb-current' : 'breadcrumb-inert'}>
                {seg.label}
              </span>
            )}
          </span>
        )
      })}
    </nav>
  )
}
