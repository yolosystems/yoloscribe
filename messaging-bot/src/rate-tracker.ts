/**
 * Rolling per-channel request counter for high-volume detection.
 * Returns true when a channel first crosses HIGH_VOLUME_THRESHOLD in a window.
 */

const WINDOW_MS = 10 * 60 * 1000 // 10 minutes
const HIGH_VOLUME_THRESHOLD = 50

const timestamps = new Map<string, number[]>()

export function recordRequest(channelId: string): boolean {
  const now = Date.now()
  const cutoff = now - WINDOW_MS
  const ts = (timestamps.get(channelId) ?? []).filter((t) => t >= cutoff)
  ts.push(now)
  timestamps.set(channelId, ts)
  return ts.length === HIGH_VOLUME_THRESHOLD + 1
}
