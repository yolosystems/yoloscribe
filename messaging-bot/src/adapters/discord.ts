/**
 * Discord platform adapter.
 *
 * Listens for messages in configured channels and routes them to the
 * YoloScribe /chat endpoint via the MessageHandler callback.
 *
 * Setup command: /yoloscribe setup <api_token>
 * Page targeting: [/page-path] message text  (defaults to root page)
 */

import crypto from 'node:crypto'
import {
  Client,
  Events,
  GatewayIntentBits,
  Partials,
  REST,
  Routes,
  SlashCommandBuilder,
  type Attachment,
  type AnyThreadChannel,
  type Interaction,
  type Message,
  type TextChannel,
} from 'discord.js'
import { DISCORD_BOT_TOKEN } from '../config.js'
import { decryptPayload, encryptPayload } from '../crypto.js'
import { recordRequest } from '../rate-tracker.js'
import { getApiTokenByHash, getConfigByChannel, upsertConfig } from '../store.js'
import type { MessageHandler, PlatformAdapter } from '../types.js'
import { triggerIngest, uploadIngestFile } from '../yoloscribe.js'

const MAX_CHARS = 2000
const PAGE_RE = /^\[\/([^\]]*)\]\s*/

function parseMessage(content: string): { filePath: string; text: string } {
  const m = PAGE_RE.exec(content)
  if (m) {
    const page = m[1].trim().replace(/^\/+/, '')
    return { filePath: page ? `${page}/content.md` : 'content.md', text: content.slice(m[0].length) }
  }
  return { filePath: 'content.md', text: content }
}

function truncate(text: string): string {
  if (text.length <= MAX_CHARS) return text
  const suffix = '\n…(truncated — see the full response in YoloScribe)'
  return text.slice(0, MAX_CHARS - suffix.length) + suffix
}

async function getOrCreateThread(message: Message): Promise<TextChannel | AnyThreadChannel> {
  if (message.channel.isThread()) return message.channel as AnyThreadChannel
  const name = message.content.length > 50 ? message.content.slice(0, 47) + '...' : message.content
  return (message.channel as TextChannel).threads.create({
    name: name || 'YoloScribe',
    autoArchiveDuration: 60,
    startMessage: message,
  })
}

export class DiscordAdapter implements PlatformAdapter {
  readonly platform = 'discord'
  private client: Client | null = null

  async start(handler: MessageHandler): Promise<void> {
    if (!DISCORD_BOT_TOKEN) throw new Error('DISCORD_BOT_TOKEN is not set')

    this.client = new Client({
      intents: [
        GatewayIntentBits.Guilds,
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.MessageContent,
      ],
      partials: [Partials.Message, Partials.Channel],
    })

    this.client.once(Events.ClientReady, async (c) => {
      console.log(`[discord] ready as ${c.user.tag}`)
      await this._registerCommands(c.user.id)
    })

    this.client.on(Events.InteractionCreate, (interaction) =>
      this._handleInteraction(interaction as Interaction).catch(console.error),
    )

    this.client.on(Events.MessageCreate, (message) =>
      this._handleMessage(message, handler).catch(console.error),
    )

    await this.client.login(DISCORD_BOT_TOKEN)
  }

  async stop(): Promise<void> {
    await this.client?.destroy()
    this.client = null
  }

  private async _registerCommands(clientId: string): Promise<void> {
    const command = new SlashCommandBuilder()
      .setName('yoloscribe')
      .setDescription('YoloScribe commands')
      .addStringOption((o) => o.setName('action').setDescription('Action (setup)').setRequired(true))
      .addStringOption((o) =>
        o.setName('api_token').setDescription('Your YoloScribe API token (as_...)').setRequired(true),
      )

    const rest = new REST().setToken(DISCORD_BOT_TOKEN)
    await rest.put(Routes.applicationCommands(clientId), { body: [command.toJSON()] })
    console.log('[discord] slash commands registered')
  }

  private async _handleInteraction(interaction: Interaction): Promise<void> {
    if (!interaction.isChatInputCommand() || interaction.commandName !== 'yoloscribe') return

    const action = interaction.options.getString('action', true)
    if (action !== 'setup') {
      await interaction.reply({ content: `Unknown action \`${action}\`. Available: \`setup\``, ephemeral: true })
      return
    }

    await interaction.deferReply({ ephemeral: true })
    const apiToken = interaction.options.getString('api_token', true)

    if (!apiToken.startsWith('as_') || apiToken.length !== 3 + 64) {
      await interaction.followUp({
        content: '❌ Invalid token format. Expected `as_<64 hex chars>`. Create a token in YoloScribe settings.',
        ephemeral: true,
      })
      return
    }

    const hash = crypto.createHash('sha256').update(apiToken).digest('hex')
    const row = await getApiTokenByHash(hash)
    if (!row) {
      await interaction.followUp({
        content: '❌ Token not found or revoked. Please generate a new token.',
        ephemeral: true,
      })
      return
    }

    const encrypted = encryptPayload(apiToken, row.site_name)
    try {
      await upsertConfig('discord', row.id, encrypted, {
        channel_id: String(interaction.channelId),
        guild_id: String(interaction.guildId ?? ''),
      })
    } catch (err) {
      console.error('[discord] setup upsert failed:', err)
      await interaction.followUp({ content: '❌ Failed to save configuration. Please try again.', ephemeral: true })
      return
    }

    await interaction.followUp({
      content: `✅ This channel is now connected to YoloScribe site **${row.site_name}**. Send any message here to chat with your wiki.`,
      ephemeral: true,
    })
  }

  private async _handleAttachments(
    message: Message,
    attachments: Attachment[],
    config: Awaited<ReturnType<typeof getConfigByChannel>>,
  ): Promise<void> {
    await message.react('⏳').catch(() => {})
    let ackEmoji = '✅'

    try {
      const { token } = decryptPayload(config!.encrypted_token)

      for (const attachment of attachments) {
        // Fetch immediately — Discord CDN URLs expire quickly
        const fileRes = await fetch(attachment.url, { signal: AbortSignal.timeout(60_000) })
        if (!fileRes.ok) throw new Error(`Failed to fetch ${attachment.name} (HTTP ${fileRes.status})`)
        const bytes = Buffer.from(await fileRes.arrayBuffer())
        const contentType = attachment.contentType ?? 'application/octet-stream'
        await uploadIngestFile(token, attachment.name, bytes, contentType)
      }

      // If the message also has text, save it as a caption alongside the first attachment
      const caption = message.content.trim()
      if (caption) {
        const captionFilename = `${attachments[0].name}.caption.txt`
        await uploadIngestFile(token, captionFilename, Buffer.from(caption, 'utf-8'), 'text/plain')
      }

      // Trigger ingest agents now that files are in the queue
      await triggerIngest(token)

      const n = attachments.length
      const thread = await getOrCreateThread(message)
      await thread.send(`✅ ${n} file${n > 1 ? 's' : ''} added to your ingest queue.`)
    } catch (err: unknown) {
      console.error('[discord] attachment upload error:', err)
      ackEmoji = '❌'
      const thread = await getOrCreateThread(message)
      await thread
        .send(`❌ Failed to upload file(s): ${err instanceof Error ? err.message : String(err)}`)
        .catch(() => {})
    }

    await message.reactions.cache.get('⏳')?.users.remove(this.client!.user!).catch(() => {})
    await message.react(ackEmoji).catch(() => {})
  }

  private async _handleMessage(message: Message, handler: MessageHandler): Promise<void> {
    if (message.author.bot) return

    const config = await getConfigByChannel('discord', String(message.channelId))
    if (!config) return

    const attachments = [...message.attachments.values()]

    if (attachments.length > 0) {
      await this._handleAttachments(message, attachments, config)
      return
    }

    const { filePath, text } = parseMessage(message.content)
    if (!text.trim()) return

    if (recordRequest(String(message.channelId))) {
      const thread = await getOrCreateThread(message)
      await thread.send('⚠️ This channel is generating high request volume. Check your rate limit headroom in the YoloScribe UI.').catch(() => {})
    }

    if (message.content.length >= MAX_CHARS) {
      const thread = await getOrCreateThread(message)
      await thread.send('⚠️ Your message is very long and may have been truncated before reaching the YoloScribe agent.').catch(() => {})
    }

    let ackEmoji = '⏳'
    await message.react('⏳').catch(() => {})

    let replyText = ''
    let outcomeEmoji = '✅'

    try {
      await handler({
        channelId: String(message.channelId),
        text,
        filePath,
        reply: async (t) => {
          const thread = await getOrCreateThread(message)
          await thread.send(truncate(t))
        },
        ack: async (emoji) => {
          const map = { thinking: '⏳', success: '✅', error: '❌', ratelimit: '🕐' } as const
          outcomeEmoji = map[emoji]
        },
        warn: async (t) => {
          const thread = await getOrCreateThread(message)
          await thread.send(t).catch(() => {})
        },
        credentials: async () => decryptPayload(config.encrypted_token),
      })
    } catch (err: unknown) {
      console.error('[discord] handler error:', err)
      outcomeEmoji = '❌'
      const thread = await getOrCreateThread(message)
      await thread.send(`❌ YoloScribe returned an error: ${err instanceof Error ? err.message : String(err)}`).catch(() => {})
    }

    await message.reactions.cache.get('⏳')?.users.remove(this.client!.user!).catch(() => {})
    await message.react(outcomeEmoji).catch(() => {})
  }
}
