/**
 * AES-256-GCM helpers for encrypting API tokens stored in messaging_configs.
 *
 * Wire format matches the Python discord-bot crypto.py:
 *   base64( nonce(12 bytes) || ciphertext+tag )
 *
 * Plaintext is JSON: {"token": "as_...", "site_name": "..."}
 */

import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto'
import { AES_KEY } from './config.js'

function key(): Buffer {
  return Buffer.from(AES_KEY, 'base64')
}

export function encryptPayload(token: string, siteName: string): string {
  const plaintext = Buffer.from(JSON.stringify({ token, site_name: siteName }), 'utf8')
  const nonce = randomBytes(12)
  const cipher = createCipheriv('aes-256-gcm', key(), nonce)
  const encrypted = Buffer.concat([cipher.update(plaintext), cipher.final()])
  const tag = cipher.getAuthTag()
  return Buffer.concat([nonce, encrypted, tag]).toString('base64')
}

export function decryptPayload(encrypted: string): { token: string; siteName: string } {
  const data = Buffer.from(encrypted, 'base64')
  const nonce = data.subarray(0, 12)
  const tag = data.subarray(data.length - 16)
  const ciphertext = data.subarray(12, data.length - 16)
  const decipher = createDecipheriv('aes-256-gcm', key(), nonce)
  decipher.setAuthTag(tag)
  const plaintext = Buffer.concat([decipher.update(ciphertext), decipher.final()])
  const payload = JSON.parse(plaintext.toString('utf8'))
  return { token: payload.token, siteName: payload.site_name }
}
