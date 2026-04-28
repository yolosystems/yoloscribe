-- Migration 001: api_tokens table
-- Stores site-scoped API tokens for programmatic access (e.g. Discord bot).
-- Only the sha256 hash of the raw token is stored; the raw value is shown to
-- the user once at creation time and cannot be retrieved again.
--
-- Apply via the Supabase SQL editor or CLI:
--   supabase db push  (if using Supabase CLI)
--   or paste into Dashboard → SQL Editor

create table if not exists api_tokens (
  id           uuid        primary key default gen_random_uuid(),
  user_id      uuid        not null references auth.users(id) on delete cascade,
  site_name    text        not null,
  name         text        not null,
  token_hash   text        not null unique,  -- sha256(raw_token), hex-encoded
  created_at   timestamptz not null default now(),
  expires_at   timestamptz,                  -- null = no expiry
  revoked_at   timestamptz,                  -- null = active
  last_used_at timestamptz
);

-- Index for fast hash lookups (used on every authenticated API request)
create index if not exists api_tokens_token_hash_idx on api_tokens (token_hash);

-- Index for listing a user's tokens
create index if not exists api_tokens_user_id_idx on api_tokens (user_id);

-- Row-level security: users may only read and delete their own rows.
-- Inserts and updates are performed exclusively via the backend service role.
alter table api_tokens enable row level security;

create policy "Users can view their own tokens"
  on api_tokens for select
  using (auth.uid() = user_id);

create policy "Users can delete their own tokens"
  on api_tokens for delete
  using (auth.uid() = user_id);
