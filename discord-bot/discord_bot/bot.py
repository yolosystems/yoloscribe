"""AgentScribe Discord bot — core bot logic.

Setup slash command: /agentscribe setup <api_token>
Message handling: any message in a configured channel is routed to POST /chat.

Page targeting syntax:
    [/page-path] your message here  →  routes to {page-path}/content.md
    your message here               →  routes to content.md (root page)

Reaction lifecycle:
    ⏳  added immediately on receipt
    ✅  swapped in on success
    ❌  swapped in on failure
    🕐  swapped in when rate-limited (instead of ❌)
"""

import hashlib
import logging
import re
from typing import Any

import discord
import httpx
from discord import app_commands

from discord_bot import crypto, rate_tracker, supabase
from discord_bot.config import AGENTSCRIBE_API_URL


class RateLimitError(Exception):
    """Raised when the AgentScribe backend returns HTTP 429."""

    def __init__(self, retry_after: str) -> None:
        self.retry_after = retry_after
        super().__init__(f"rate limit reached (retry after {retry_after}s)")

log = logging.getLogger(__name__)

# Discord message length limit.
_DISCORD_MAX_CHARS = 2000

# Regex to parse optional [/page] prefix from a message.
_PAGE_RE = re.compile(r"^\[(/[^\]]*)\]\s*")


def parse_message(content: str) -> tuple[str, str]:
    """Return (file_path, message_text).

    If the message starts with [/page], returns "{page}/content.md" and the
    remaining text. Otherwise returns "content.md" and the full text.
    """
    m = _PAGE_RE.match(content)
    if m:
        page = m.group(1).strip("/")
        file_path = f"{page}/content.md" if page else "content.md"
        return file_path, content[m.end():]
    return "content.md", content


def truncate_response(text: str) -> str:
    """Truncate a response to fit Discord's 2000-char limit."""
    if len(text) <= _DISCORD_MAX_CHARS:
        return text
    suffix = "\n…(truncated — see the full response in AgentScribe)"
    return text[: _DISCORD_MAX_CHARS - len(suffix)] + suffix


class AgentScribeBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # Privileged intent — must be enabled in the Dev Portal
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(
            name="agentscribe",
            description="AgentScribe commands",
        )
        @app_commands.describe(action="Action to perform (setup)")
        @app_commands.describe(api_token="Your AgentScribe API token (as_...)")
        async def agentscribe(
            interaction: discord.Interaction,
            action: str,
            api_token: str,
        ) -> None:
            if action != "setup":
                await interaction.response.send_message(
                    f"Unknown action `{action}`. Available: `setup`",
                    ephemeral=True,
                )
                return
            await self._handle_setup(interaction, api_token)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        log.info("Slash commands synced globally")

    async def on_ready(self) -> None:
        log.info("AgentScribe bot ready as %s (id=%s)", self.user, self.user.id)  # type: ignore[union-attr]

    # ── Setup command ──────────────────────────────────────────────────────────

    async def _handle_setup(
        self, interaction: discord.Interaction, api_token: str
    ) -> None:
        """Validate and store an API token for this channel."""
        await interaction.response.defer(ephemeral=True)

        # Basic format check before touching Supabase.
        if not (api_token.startswith("as_") and len(api_token) == 3 + 64):
            await interaction.followup.send(
                "❌ Invalid token format. Expected `as_<64 hex chars>`. "
                "Create a token at your AgentScribe site settings.",
                ephemeral=True,
            )
            return

        token_hash = hashlib.sha256(api_token.encode()).hexdigest()
        row = supabase.get_api_token_by_hash(token_hash)
        if row is None:
            await interaction.followup.send(
                "❌ Token not found or already revoked. Please generate a new token.",
                ephemeral=True,
            )
            return

        encrypted = crypto.encrypt_payload(api_token, row["site_name"])
        try:
            supabase.upsert_discord_config(
                channel_id=str(interaction.channel_id),
                guild_id=str(interaction.guild_id),
                api_token_id=row["id"],
                encrypted_token=encrypted,
            )
        except Exception as exc:
            log.error("Failed to upsert discord_config: %s", exc)
            await interaction.followup.send(
                "❌ Failed to save configuration. Please try again.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ This channel is now connected to AgentScribe site **{row['site_name']}**. "
            "Send any message here to chat with your wiki.",
            ephemeral=True,
        )

    # ── Message handling ───────────────────────────────────────────────────────

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        config = supabase.get_discord_config(str(message.channel.id))
        if config is None:
            return  # Channel not configured — ignore

        try:
            raw_token, site_name = crypto.decrypt_payload(config["encrypted_token"])
        except Exception as exc:
            log.error("Failed to decrypt token for channel %s: %s", message.channel.id, exc)
            await message.reply(
                "⚠️ Failed to decrypt stored token. "
                "Please re-run `/agentscribe setup` with a valid token."
            )
            return

        file_path, text = parse_message(message.content)

        if not text.strip():
            return  # Empty message after stripping prefix — nothing to send

        # Track request volume; post high-volume warning if threshold just crossed.
        if rate_tracker.record_request(str(message.channel.id)):
            try:
                thread = await _get_or_create_thread(message)
                await thread.send(
                    "⚠️ This channel is generating high request volume. "
                    "Check your rate limit headroom in the AgentScribe UI."
                )
            except discord.HTTPException:
                pass

        # Add hourglass reaction immediately so the user knows we received it.
        try:
            await message.add_reaction("⏳")
        except discord.HTTPException:
            pass  # Non-fatal — continue even if reaction fails

        # Warn if the message was very long (Discord clips at 2000 chars on send
        # but bots may receive longer content in some contexts).
        if len(message.content) >= _DISCORD_MAX_CHARS:
            try:
                thread = await _get_or_create_thread(message)
                await thread.send(
                    "⚠️ Your message is very long and may have been truncated "
                    "before reaching the AgentScribe agent."
                )
            except discord.HTTPException:
                pass

        outcome_reaction = "✅"
        reply_text = ""
        try:
            reply_text = await self._call_chat(
                raw_token=raw_token,
                site=site_name,
                file_path=file_path,
                message=text,
            )
        except RateLimitError as exc:
            log.warning("Rate limit for channel %s: retry after %s", message.channel.id, exc.retry_after)
            outcome_reaction = "🕐"
            reply_text = (
                f"Rate limit reached. "
                f"You can send another message in {exc.retry_after} seconds."
            )
        except Exception as exc:
            log.error("Chat API error for channel %s: %s", message.channel.id, exc)
            outcome_reaction = "❌"
            reply_text = f"❌ AgentScribe returned an error: {exc}"

        # Post response in a thread on the original message.
        try:
            thread = await _get_or_create_thread(message)
            await thread.send(truncate_response(reply_text))
        except discord.HTTPException as exc:
            log.error("Failed to post reply in thread: %s", exc)

        # Swap the ⏳ for the outcome reaction.
        try:
            await message.remove_reaction("⏳", self.user)  # type: ignore[arg-type]
            await message.add_reaction(outcome_reaction)
        except discord.HTTPException:
            pass

    async def _call_chat(
        self,
        raw_token: str,
        site: str,
        file_path: str,
        message: str,
    ) -> str:
        """POST to the AgentScribe /chat endpoint and return the reply text."""
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{AGENTSCRIBE_API_URL}/chat",
                headers={"Authorization": f"Bearer {raw_token}"},
                json={
                    "message": message,
                    "current_content": "",
                    "history": [],
                    "site": site,
                    "file_path": file_path,
                },
            )
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "unknown")
            raise RateLimitError(retry_after)
        resp.raise_for_status()
        return resp.json().get("reply", "")


async def _get_or_create_thread(message: discord.Message) -> Any:
    """Return the existing thread for a message, or create one."""
    if isinstance(message.channel, discord.Thread):
        return message.channel
    # Create a thread named after the first 50 chars of the message content.
    thread_name = (message.content[:47] + "...") if len(message.content) > 50 else message.content
    return await message.create_thread(name=thread_name or "AgentScribe", auto_archive_duration=60)
