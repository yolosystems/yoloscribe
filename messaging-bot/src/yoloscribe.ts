/** YoloScribe API client — fetch page content and send chat messages. */

import { YOLOSCRIBE_API_URL } from './config.js'

export class RateLimitError extends Error {
  constructor(public readonly retryAfter: string) {
    super(`Rate limit reached (retry after ${retryAfter}s)`)
  }
}

/** Fetch the current content of a wiki page. Returns empty string on failure. */
export async function fetchContent(
  token: string,
  site: string,
  filePath: string,
): Promise<string> {
  try {
    const resp = await fetch(
      `${YOLOSCRIBE_API_URL}/content?site=${encodeURIComponent(site)}&path=${encodeURIComponent(filePath)}`,
      { headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(10_000) },
    )
    if (resp.ok) return resp.text()
  } catch {
    // best-effort
  }
  return ''
}

/** POST to /chat and return the reply text. Throws RateLimitError on 429. */
export async function sendChat(
  token: string,
  site: string,
  filePath: string,
  message: string,
  currentContent: string,
): Promise<string> {
  const resp = await fetch(`${YOLOSCRIBE_API_URL}/chat`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      message,
      current_content: currentContent,
      history: [],
      site,
      file_path: filePath,
    }),
    signal: AbortSignal.timeout(120_000),
  })
  if (resp.status === 429) {
    throw new RateLimitError(resp.headers.get('Retry-After') ?? 'unknown')
  }
  if (!resp.ok) throw new Error(`YoloScribe /chat returned ${resp.status}`)
  const data = await resp.json() as { reply?: string }
  return data.reply ?? ''
}
