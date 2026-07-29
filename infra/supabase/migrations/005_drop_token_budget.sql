-- Migration 005: drop the deprecated token-budget tables (YOL-513)
--
-- Token budgets are now enforced by LiteLLM per-user virtual keys (the proxy
-- meters spend and returns 429 when exhausted). The Supabase token_budgets /
-- token_usage tables and the increment_token_usage RPC created in migration 003
-- are no longer read or written by the backend or the agent runner.
--
-- Apply via the Supabase SQL editor or CLI (supabase db push).

drop function if exists increment_token_usage(uuid, date, integer);
drop table if exists token_usage;   -- drops its index + RLS policy
drop table if exists token_budgets; -- drops its RLS policy
