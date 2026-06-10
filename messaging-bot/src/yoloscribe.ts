/** YoloScribe API client — messaging and ingest queue operations. */

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

/**
 * Upload a file to the site's ingest queue via the pre-signed S3 PUT flow.
 * Step 1: POST /ingest/upload to get a pre-signed URL.
 * Step 2: PUT the file bytes directly to S3.
 */
export async function uploadIngestFile(
  token: string,
  filename: string,
  bytes: Uint8Array,
  contentType: string,
): Promise<void> {
  const params = new URLSearchParams({ filename })
  const resp = await fetch(`${YOLOSCRIBE_API_URL}/ingest/upload?${params}`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
    signal: AbortSignal.timeout(30_000),
  })
  if (!resp.ok) throw new Error(`/ingest/upload returned ${resp.status}`)
  const { upload_url } = (await resp.json()) as { upload_url: string }

  const putResp = await fetch(upload_url, {
    method: 'PUT',
    headers: { 'Content-Type': contentType },
    body: new Blob([new Uint8Array(bytes)], { type: contentType }),
    signal: AbortSignal.timeout(120_000),
  })
  if (!putResp.ok) throw new Error(`S3 PUT returned ${putResp.status}`)
}
