import { useState, useEffect } from 'react'

interface Props {
  apiBase: string
  site: string
  pagePath: string
  onNavigate: (filePath: string) => void
}

export default function ChildPagesList({ apiBase, site, pagePath, onNavigate }: Props) {
  const [pages, setPages] = useState<string[]>([])

  useEffect(() => {
    const url = `${apiBase}/pages?site=${encodeURIComponent(site)}&page_path=${encodeURIComponent(pagePath)}`
    fetch(url)
      .then((res) => (res.ok ? res.json() : { pages: [] }))
      .then((data) => setPages(data.pages ?? []))
      .catch(() => setPages([]))
  }, [apiBase, site, pagePath])

  if (pages.length === 0) return null

  return (
    <div className="child-pages-list">
      <div className="child-pages-header">Pages</div>
      {pages.map((page) => {
        const fullPath = pagePath ? `${pagePath}/${page}` : page
        return (
          <button
            key={page}
            className="child-pages-item"
            onClick={() => onNavigate(`${fullPath}/content.md`)}
          >
            {page}
          </button>
        )
      })}
    </div>
  )
}
