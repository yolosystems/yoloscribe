/** Environment configuration for the messaging bot. */

function required(name: string): string {
  const val = process.env[name]
  if (!val) throw new Error(`Missing required env var: ${name}`)
  return val
}

function optional(name: string, fallback = ''): string {
  return process.env[name] ?? fallback
}

export const YOLOSCRIBE_API_URL = required('YOLOSCRIBE_API_URL').replace(/\/$/, '')
export const SUPABASE_URL = required('SUPABASE_URL').replace(/\/$/, '')
export const SUPABASE_SERVICE_ROLE_KEY = required('SUPABASE_SERVICE_ROLE_KEY')

// Base64-encoded 32-byte AES-256 key for encrypting API tokens at rest.
// Same format as the Python discord-bot: base64(nonce(12) || ciphertext+tag).
// Generate: node -e "console.log(require('crypto').randomBytes(32).toString('base64'))"
export const AES_KEY = required('MESSAGING_AES_KEY')

// Comma-separated list of enabled platform adapters, e.g. "discord,slack"
export const ENABLED_ADAPTERS = optional('ENABLED_ADAPTERS', 'discord')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean)

// Platform-specific tokens (only required when the adapter is enabled)
export const DISCORD_BOT_TOKEN = optional('DISCORD_BOT_TOKEN')
