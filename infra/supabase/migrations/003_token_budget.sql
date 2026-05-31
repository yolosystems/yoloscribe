-- Migration 003: token_budgets + token_usage tables
-- Tracks per-user daily token consumption and optional per-user budget overrides.
-- The platform default limit is TOKEN_BUDGET_DEFAULT_DAILY_LIMIT (env var; default 500,000).
-- Per-user overrides are set by inserting or updating a row in token_budgets directly
-- via the Supabase dashboard or SQL editor.
--
-- Apply via the Supabase SQL editor or CLI:
--   supabase db push  (if using Supabase CLI)
--   or paste into Dashboard → SQL Editor

-- Per-user daily limit overrides.  Absence of a row means use the platform default.
create table if not exists token_budgets (
  user_id     uuid    primary key references auth.users(id) on delete cascade,
  daily_limit integer not null check (daily_limit > 0)
);

-- Daily token usage counters, one row per (user, date).
create table if not exists token_usage (
  user_id      uuid    not null references auth.users(id) on delete cascade,
  usage_date   date    not null,
  total_tokens integer not null default 0 check (total_tokens >= 0),
  primary key (user_id, usage_date)
);

create index if not exists token_usage_user_id_idx on token_usage (user_id);

-- Atomic upsert used by both the backend and the agent runner to record usage.
-- Safe under concurrent writes: increments total_tokens rather than overwriting it.
create or replace function increment_token_usage(
  p_user_id uuid,
  p_date    date,
  p_tokens  integer
) returns void language plpgsql security definer as $$
begin
  insert into token_usage (user_id, usage_date, total_tokens)
  values (p_user_id, p_date, p_tokens)
  on conflict (user_id, usage_date)
  do update set total_tokens = token_usage.total_tokens + excluded.total_tokens;
end;
$$;

-- RLS: users may read their own rows; all writes go through the service role or
-- the security-definer function above.
alter table token_budgets enable row level security;
alter table token_usage    enable row level security;

create policy "Users can view their own budget"
  on token_budgets for select
  using (auth.uid() = user_id);

create policy "Users can view their own usage"
  on token_usage for select
  using (auth.uid() = user_id);
