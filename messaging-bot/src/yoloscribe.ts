/**
 * YoloScribe backend client.
 *
 * Every call goes to the backend's /internal/messaging/* endpoints, authenticated
 * with the bot's own shared secret and identified by a platform channel. The
 * backend resolves channel → api_token_id → (user_id, site) and runs the request
 * as that owner.
 *
 * The bot therefore never holds a user's API token, an encryption key, or any
 * database credential (YOL-523). The one secret it does hold is useless from
 * outside the cluster, since /internal/* is blocked at the ALB.
 */

import { MESSAGING_BOT_SECRET, YOLOSCRIBE_API_URL } from './config.js'

export class RateLimitError extends Error {
  constructor(public readonly retryAfter: string) {
    super(`Rate limit reached (retry after ${retryAfter}s)`)
  }
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  return { 'X-Internal-Auth': MESSAGING_BOT_SECRET, ...extra }
}

function jsonHeaders(): Record<string, string> {
  return headers({ 'Content-Type': 'application/json' })
}

/**
 * Bind a channel to the site behind an API token, during the /setup flow.
 *
 * This is the only place a user's API token is handled, and it is forwarded
 * straight to the backend rather than stored — the binding the backend writes
 * records only the token's ID.
 *
 * Returns the site name for the confirmation message, or null if the token was
 * rejected as invalid, revoked, or expired.
 */
export async function linkChannel(
  platform: string,
  channelId: string,
  apiToken: string,
  connection: Record<string, string> = {},
): Promise<string | null> {
  const resp = await fetch(`${YOLOSCRIBE_API_URL}/internal/messaging/link`, {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify({ platform, channel_id: channelId, api_token: apiToken, connection }),
    signal: AbortSignal.timeout(15_000),
  })
  if (resp.status === 401) return null
  if (!resp.ok) throw new Error(`/internal/messaging/link returned ${resp.status}`)
  const data = (await resp.json()) as { site_name?: string }
  return data.site_name ?? null
}

/** Report whether a channel is linked, so the adapter knows to respond. */
export async function isChannelLinked(platform: string, channelId: string): Promise<boolean> {
  const params = new URLSearchParams({ platform, channel_id: channelId })
  const resp = await fetch(`${YOLOSCRIBE_API_URL}/internal/messaging/binding?${params}`, {
    headers: headers(),
    signal: AbortSignal.timeout(15_000),
  })
  if (!resp.ok) return false
  const data = (await resp.json()) as { linked?: boolean }
  return data.linked === true
}

/**
 * Send a message on behalf of the channel's owner. The backend loads
 * conversation history, calls the MessagingAgent, and returns the reply.
 */
export async function sendMessage(
  platform: string,
  channelId: string,
  message: string,
): Promise<string> {
  const resp = await fetch(`${YOLOSCRIBE_API_URL}/internal/messaging/message`, {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify({ platform, channel_id: channelId, message }),
    signal: AbortSignal.timeout(120_000),
  })
  if (resp.status === 429) {
    throw new RateLimitError(resp.headers.get('Retry-After') ?? 'unknown')
  }
  if (!resp.ok) throw new Error(`/internal/messaging/message returned ${resp.status}`)
  const data = (await resp.json()) as { reply?: string }
  return data.reply ?? ''
}

/**
 * Upload a file to the channel site's ingest queue via the pre-signed S3 PUT flow.
 * Step 1: ask the backend for a pre-signed URL. Step 2: PUT the bytes to S3.
 */
export async function uploadIngestFile(
  platform: string,
  channelId: string,
  filename: string,
  bytes: Uint8Array,
  contentType: string,
): Promise<void> {
  const params = new URLSearchParams({ filename })
  const resp = await fetch(
    `${YOLOSCRIBE_API_URL}/internal/messaging/ingest/upload?${params}`,
    {
      method: 'POST',
      headers: jsonHeaders(),
      body: JSON.stringify({ platform, channel_id: channelId }),
      signal: AbortSignal.timeout(30_000),
    },
  )
  if (!resp.ok) throw new Error(`/internal/messaging/ingest/upload returned ${resp.status}`)
  const { upload_url } = (await resp.json()) as { upload_url: string }

  const putResp = await fetch(upload_url, {
    method: 'PUT',
    headers: { 'Content-Type': contentType },
    body: new Blob([new Uint8Array(bytes)], { type: contentType }),
    signal: AbortSignal.timeout(120_000),
  })
  if (!putResp.ok) throw new Error(`S3 PUT returned ${putResp.status}`)
}

/** Trigger on_write ingest agents for the channel's site, after uploads finish. */
export async function triggerIngest(platform: string, channelId: string): Promise<void> {
  const resp = await fetch(`${YOLOSCRIBE_API_URL}/internal/messaging/ingest/trigger`, {
    method: 'POST',
    headers: jsonHeaders(),
    body: JSON.stringify({ platform, channel_id: channelId }),
    signal: AbortSignal.timeout(15_000),
  })
  if (!resp.ok) throw new Error(`/internal/messaging/ingest/trigger returned ${resp.status}`)
}
