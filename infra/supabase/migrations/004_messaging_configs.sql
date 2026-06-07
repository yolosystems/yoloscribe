-- messaging_configs: platform-agnostic channel→token mapping for the messaging bot.
--
-- Replaces the Discord-specific discord_configs table for the new messaging-bot
-- service. The existing discord_configs table remains intact during the transition
-- period while both services run in parallel.
--
-- The `connection` JSONB column holds platform-specific identifiers:
--   Discord:  { "channel_id": "...", "guild_id": "..." }
--   Slack:    { "channel_id": "...", "workspace_id": "..." }
--   Telegram: { "chat_id": "..." }

create table if not exists messaging_configs (
  id              uuid primary key default gen_random_uuid(),
  platform        text not null,
  api_token_id    uuid not null references api_tokens(id) on delete cascade,
  encrypted_token text not null,
  connection      jsonb not null default '{}',
  created_at      timestamptz not null default now()
);

-- One config per platform+channel (channel_id is the universal key across platforms).
create unique index if not exists messaging_configs_platform_channel_uniq
  on messaging_configs (platform, (connection->>'channel_id'));

-- RLS: service role only (same pattern as discord_configs).
-- The bot service uses the service role key; end users manage connections
-- via the YoloScribe backend API, not directly.
alter table messaging_configs enable row level security;

-- Allow the backend (service role) full access; no user-level RLS policies needed
-- since the backend enforces site ownership before proxying these operations.
