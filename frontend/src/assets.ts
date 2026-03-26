/**
 * Asset URL resolution for YoloScribe (YOL-130).
 *
 * Rules:
 *  - Images always go through the backend GET /asset (access-controlled proxy).
 *  - Video/audio go directly to CloudFront when VITE_CLOUDFRONT_MEDIA_DOMAIN is set
 *    and LOCAL_MODE is false — the browser attaches the signed cookies automatically.
 *  - In LOCAL_MODE all asset types are proxied through GET /asset (MinIO backend).
 *
 * Relative paths (e.g. "assets/foo.png", "intro/assets/bar.mp4") are resolved
 * relative to the current site.  Absolute paths (starting with "/") are passed
 * through unchanged to CloudFront or the backend as appropriate.
 */

const LOCAL_MODE = import.meta.env.VITE_LOCAL_MODE === 'true'
const CLOUDFRONT_MEDIA_DOMAIN = import.meta.env.VITE_CLOUDFRONT_MEDIA_DOMAIN ?? ''

const VIDEO_RE = /\.(mp4|m4v)$/i
const AUDIO_RE = /\.m4a$/i

function isMediaAsset(src: string): boolean {
  return VIDEO_RE.test(src) || AUDIO_RE.test(src)
}

/**
 * Resolve a markdown image `src` to a full URL for use in the browser.
 *
 * @param src      - The raw src from the markdown (e.g. "assets/demo.mp4")
 * @param site     - The current site name (e.g. "knuth-home")
 * @param apiBase  - The API base URL (e.g. "/api" or "https://api.example.com/api")
 */
export function resolveAssetUrl(src: string, site: string, apiBase: string): string {
  if (!src) return src

  // Pass through absolute http(s) URLs unchanged — these are external images.
  if (/^https?:\/\//i.test(src)) return src

  // In LOCAL_MODE all assets (including video/audio) go through the backend.
  if (LOCAL_MODE) {
    const path = src.startsWith('/') ? src.slice(1) : src
    return `${apiBase}/asset?site=${encodeURIComponent(site)}&path=${encodeURIComponent(path)}`
  }

  // Production: video/audio go to CloudFront when the domain is configured.
  if (isMediaAsset(src) && CLOUDFRONT_MEDIA_DOMAIN) {
    if (src.startsWith('/')) {
      // Absolute asset path — prepend site prefix.
      return `https://${CLOUDFRONT_MEDIA_DOMAIN}/${site}${src}`
    }
    return `https://${CLOUDFRONT_MEDIA_DOMAIN}/${site}/${src}`
  }

  // Images (and video/audio when CloudFront is not configured) go through the backend.
  const path = src.startsWith('/') ? src.slice(1) : src
  return `${apiBase}/asset?site=${encodeURIComponent(site)}&path=${encodeURIComponent(path)}`
}
