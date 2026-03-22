# Supabase Migrations

SQL migration files for the YoloScribe Supabase project.

## Applying Migrations

Migrations must be applied in numbered order. Use the Supabase Dashboard SQL Editor or the Supabase CLI:

```bash
# Supabase CLI (if configured)
supabase db push

# Or paste each file manually into:
# Dashboard → SQL Editor → New query → Run
```

## Migration History

| File | Description | Depends On |
|------|-------------|------------|
| `001_api_tokens.sql` | API token table with RLS | — |
| `002_discord_configs.sql` | Discord channel → site mapping | 001 |
