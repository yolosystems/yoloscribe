"""Environment configuration for the YoloScribe Discord bot."""

import os

DISCORD_BOT_TOKEN: str = os.environ["DISCORD_BOT_TOKEN"]

# Base64-encoded 32-byte key for AES-256-GCM encryption of stored API tokens.
# Generate with: python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
DISCORD_AES_KEY: str = os.environ["DISCORD_AES_KEY"]

# YoloScribe backend API base URL (e.g. https://yoloscribe-dev.runyolo.dev)
YOLOSCRIBE_API_URL: str = os.environ["YOLOSCRIBE_API_URL"].rstrip("/")

# Supabase credentials — used by the bot to look up api_tokens and
# upsert discord_configs rows. Service role key bypasses RLS.
SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
