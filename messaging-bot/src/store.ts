/**
 * Supabase PostgREST helpers for the messaging bot.
 *
 * Uses the messaging_configs table (platform-agnostic, replaces discord_configs).
 * Falls back to querying api_tokens by hash during the setup flow.
 */

import { SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY } from './config.js'

function headers(): Record<string, string> {
  return {
    Authorization: `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`,
    apikey: SUPABASE_SERVICE_ROLE_KEY,
    'Content-Type': 'application/json',
  }
}

export interface ApiTokenRow {
  id: string
  site_name: string
  expires_at: string | null
}

export interface MessagingConfigRow {
  id: string
  encrypted_token: string
  connection: Record<string, string>
}

/** Look up an api_tokens row by hash. Returns null if not found or revoked. */
export async function getApiTokenByHash(hash: string): Promise<ApiTokenRow | null> {
  const qs = new URLSearchParams({
    token_hash: `eq.${hash}`,
    revoked_at: 'is.null',
    select: 'id,site_name,expires_at',
    limit: '1',
  })
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/api_tokens?${qs}`, { headers: headers() })
  if (!resp.ok) return null
  const rows: ApiTokenRow[] = await resp.json()
  return rows[0] ?? null
}

/** Upsert a messaging_configs row. Conflicts on (platform, connection->>'channel_id'). */
export async function upsertConfig(
  platform: string,
  apiTokenId: string,
  encryptedToken: string,
  connection: Record<string, string>,
): Promise<void> {
  const row = { platform, api_token_id: apiTokenId, encrypted_token: encryptedToken, connection }
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/messaging_configs`, {
    method: 'POST',
    headers: {
      ...headers(),
      Prefer: 'resolution=merge-duplicates,return=minimal',
    },
    body: JSON.stringify(row),
  })
  if (!resp.ok) {
    const body = await resp.text()
    throw new Error(`Failed to upsert messaging_config: ${resp.status} ${body}`)
  }
}

/** Look up a messaging_configs row by platform + channel_id. */
export async function getConfigByChannel(
  platform: string,
  channelId: string,
): Promise<MessagingConfigRow | null> {
  const qs = new URLSearchParams({
    platform: `eq.${platform}`,
    'connection->>channel_id': `eq.${channelId}`,
    select: 'id,encrypted_token,connection',
    limit: '1',
  })
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/messaging_configs?${qs}`, {
    headers: headers(),
  })
  if (!resp.ok) return null
  const rows: MessagingConfigRow[] = await resp.json()
  return rows[0] ?? null
}

/** List all messaging_configs rows for a given API token (for frontend display). */
export async function listConfigsByTokenIds(tokenIds: string[]): Promise<MessagingConfigRow[]> {
  if (tokenIds.length === 0) return []
  const qs = new URLSearchParams({
    api_token_id: `in.(${tokenIds.join(',')})`,
    select: 'id,platform,connection,created_at,api_token_id',
  })
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/messaging_configs?${qs}`, {
    headers: headers(),
  })
  if (!resp.ok) return []
  return resp.json()
}

/** Delete a messaging_configs row by id. */
export async function deleteConfig(id: string): Promise<void> {
  const resp = await fetch(
    `${SUPABASE_URL}/rest/v1/messaging_configs?id=eq.${encodeURIComponent(id)}`,
    { method: 'DELETE', headers: headers() },
  )
  if (!resp.ok) throw new Error(`Failed to delete messaging_config ${id}: ${resp.status}`)
}
