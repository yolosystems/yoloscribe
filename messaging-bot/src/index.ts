/**
 * YoloScribe Messaging Bot — entry point.
 *
 * Loads each enabled platform adapter and starts listening for messages.
 * Each incoming message is routed to the YoloScribe /chat endpoint using
 * the API token stored for that channel.
 */

import { ENABLED_ADAPTERS } from './config.js'
import type { MessageHandler, PlatformAdapter } from './types.js'
import { sendMessage, RateLimitError } from './yoloscribe.js'

async function handleMessage(adapter: PlatformAdapter): Promise<MessageHandler> {
  return async (msg) => {
    const { token } = await msg.credentials()

    try {
      const reply = await sendMessage(token, adapter.platform, msg.channelId, msg.text)
      await msg.reply(reply)
      await msg.ack('success')
    } catch (err) {
      if (err instanceof RateLimitError) {
        await msg.ack('ratelimit')
        await msg.reply(`Rate limit reached. You can send another message in ${err.retryAfter} seconds.`)
      } else {
        await msg.ack('error')
        throw err
      }
    }
  }
}

async function loadAdapters(): Promise<PlatformAdapter[]> {
  const adapters: PlatformAdapter[] = []
  for (const platform of ENABLED_ADAPTERS) {
    try {
      const mod = await import(`./adapters/${platform}.js`)
      const AdapterClass = Object.values(mod).find(
        (v) => typeof v === 'function' && 'prototype' in v,
      ) as new () => PlatformAdapter
      adapters.push(new AdapterClass())
      console.log(`[bot] loaded adapter: ${platform}`)
    } catch (err) {
      console.error(`[bot] failed to load adapter "${platform}":`, err)
    }
  }
  return adapters
}

async function main() {
  console.log(`[bot] starting with adapters: ${ENABLED_ADAPTERS.join(', ')}`)
  const adapters = await loadAdapters()

  if (adapters.length === 0) {
    console.error('[bot] no adapters loaded — check ENABLED_ADAPTERS and adapter tokens')
    process.exit(1)
  }

  for (const adapter of adapters) {
    const handler = await handleMessage(adapter)
    await adapter.start(handler)
    console.log(`[bot] ${adapter.platform} adapter started`)
  }

  process.on('SIGTERM', async () => {
    console.log('[bot] shutting down...')
    await Promise.all(adapters.map((a) => a.stop()))
    process.exit(0)
  })
}

main().catch((err) => {
  console.error('[bot] fatal error:', err)
  process.exit(1)
})
