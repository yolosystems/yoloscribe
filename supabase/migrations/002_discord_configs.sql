-- Migration 002: discord_configs table
-- Links a Discord channel to an YoloScribe site via a stored API token.
-- The raw API token is AES-encrypted before storage; the bot decrypts it at
-- runtime using a key held in a Kubernetes Secret.
--
-- Depends on: 001_api_tokens.sql (api_tokens table must exist first)
--
-- Apply via the Supabase SQL editor or CLI after 001_api_tokens.sql.

create table if not exists discord_configs (
  id               uuid        primary key default gen_random_uuid(),
  guild_id         text        not null,
  channel_id       text        not null unique,  -- one site per channel
  api_token_id     uuid        not null references api_tokens(id) on delete cascade,
  encrypted_token  text        not null,          -- AES-GCM encrypted raw token
  created_at       timestamptz not null default now()
);

-- Index for fast channel-id lookups (used on every incoming Discord message)
create index if not exists discord_configs_channel_id_idx on discord_configs (channel_id);

-- Index for cascading cleanup when a token is revoked
create index if not exists discord_configs_api_token_id_idx on discord_configs (api_token_id);

-- Row-level security: this table is only accessed via the backend service role
-- and the bot service; no direct client access is permitted.
alter table discord_configs enable row level security;

-- No user-facing RLS policies — all access is via service role key.
-- The cascade on api_token_id ensures discord_configs rows are removed
-- automatically when their linked api_token is deleted (revoked).
