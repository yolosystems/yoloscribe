/** YoloScribe API client — send messages via the POST /message endpoint. */

import { YOLOSCRIBE_API_URL } from './config.js'

export class RateLimitError extends Error {
  constructor(public readonly retryAfter: string) {
    super(`Rate limit reached (retry after ${retryAfter}s)`)
  }
}

/**
 * Send a message to the YoloScribe /message endpoint.
 * The server resolves the site from the API token, loads conversation history,
 * and returns a reply from the MessagingAgent.
 */
export async function sendMessage(
  token: string,
  platform: string,
  channelId: string,
  message: string,
): Promise<string> {
  const resp = await fetch(`${YOLOSCRIBE_API_URL}/message`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ platform, channel_id: channelId, message }),
    signal: AbortSignal.timeout(120_000),
  })
  if (resp.status === 429) {
    throw new RateLimitError(resp.headers.get('Retry-After') ?? 'unknown')
  }
  if (!resp.ok) throw new Error(`YoloScribe /message returned ${resp.status}`)
  const data = (await resp.json()) as { reply?: string }
  return data.reply ?? ''
}
