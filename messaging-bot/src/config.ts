/** Environment configuration for the messaging bot. */

function required(name: string): string {
  const val = process.env[name]
  if (!val) throw new Error(`Missing required env var: ${name}`)
  return val
}

function optional(name: string, fallback = ''): string {
  return process.env[name] ?? fallback
}

// Base URL of the YoloScribe backend. The bot calls the backend's /internal/*
// endpoints, which are blocked at the ALB (see infra/waf/README.md), so this
// must be the in-cluster service DNS name — e.g.
// http://yoloscribe-backend.yolo.svc.cluster.local:8000 — not the public host.
export const YOLOSCRIBE_API_URL = required('YOLOSCRIBE_API_URL').replace(/\/$/, '')

// Shared secret for the backend's /internal/messaging/* endpoints, sent as the
// X-Internal-Auth header.
//
// Deliberately NOT the same value as the backend's INTERNAL_MINT_SECRET: this
// process handles untrusted input from chat platforms, and /internal/runs/mint
// accepts an arbitrary site + user_id. Keeping them separate means a compromised
// bot cannot mint run tokens (YOL-523).
export const MESSAGING_BOT_SECRET = required('MESSAGING_BOT_SECRET')

// Comma-separated list of enabled platform adapters, e.g. "discord,slack"
export const ENABLED_ADAPTERS = optional('ENABLED_ADAPTERS', 'discord')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean)

// Platform-specific tokens (only required when the adapter is enabled)
export const DISCORD_BOT_TOKEN = optional('DISCORD_BOT_TOKEN')
