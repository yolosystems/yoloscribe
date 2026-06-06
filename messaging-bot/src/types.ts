/**
 * Platform adapter interface for the YoloScribe messaging bot.
 *
 * Each supported platform (Discord, Slack, Telegram…) implements PlatformAdapter.
 * The core service calls start(), passing a MessageHandler. The adapter calls
 * the handler for every message it receives in a configured channel.
 */

export type AckEmoji = 'thinking' | 'success' | 'error' | 'ratelimit'

export interface IncomingMessage {
  /** Platform-specific channel identifier (e.g. Discord channel ID). */
  channelId: string
  /** Parsed message text, with any platform-specific prefix stripped. */
  text: string
  /**
   * Wiki file path derived from the message (e.g. "projects/foo/content.md"),
   * or "content.md" for the root page. Parsed from [/page] prefix syntax.
   */
  filePath: string
  /** Post a reply in the same thread/channel. */
  reply(text: string): Promise<void>
  /** Update the acknowledgement reaction on the original message. */
  ack(emoji: AckEmoji): Promise<void>
  /** Post an optional warning (e.g. message near length limit). */
  warn(text: string): Promise<void>
  /** Retrieve the decrypted API token and site name for this channel. */
  credentials(): Promise<{ token: string; siteName: string }>
}

export type MessageHandler = (msg: IncomingMessage) => Promise<void>

export interface PlatformAdapter {
  /** Short platform identifier — matches the platform column in messaging_configs. */
  readonly platform: string
  /** Connect to the platform and start delivering messages to handler. */
  start(handler: MessageHandler): Promise<void>
  /** Disconnect gracefully. */
  stop(): Promise<void>
}
