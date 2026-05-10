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
 * @param src      - The raw src from the markdown (e.g. "media/demo.mp4")
 * @param site     - The current site name (e.g. "knuth")
 * @param apiBase  - The API base URL (e.g. "/api" or "https://api.example.com/api")
 * @param pagePath - The current page path (e.g. "intro" or "intro/sub"); used to
 *                   resolve relative asset paths to their full S3 key.
 */
export function resolveAssetUrl(src: string, site: string, apiBase: string, pagePath = ''): string {
  if (!src) return src

  // Pass through absolute http(s) URLs unchanged — these are external images.
  if (/^https?:\/\//i.test(src)) return src

  // Resolve relative paths against the current page.
  // e.g. src="media/foo.mp4", pagePath="intro/sub" → fullPath="intro/sub/media/foo.mp4"
  const fullPath = src.startsWith('/')
    ? src.slice(1)
    : pagePath ? `${pagePath}/${src}` : src

  // In LOCAL_MODE all assets (including video/audio) go through the backend.
  if (LOCAL_MODE) {
    return `${apiBase}/asset?site=${encodeURIComponent(site)}&path=${encodeURIComponent(fullPath)}`
  }

  // Production: video/audio go to CloudFront when the domain is configured.
  if (isMediaAsset(src) && CLOUDFRONT_MEDIA_DOMAIN) {
    return `https://${CLOUDFRONT_MEDIA_DOMAIN}/${site}/${fullPath}`
  }

  // Images (and video/audio when CloudFront is not configured) go through the backend.
  return `${apiBase}/asset?site=${encodeURIComponent(site)}&path=${encodeURIComponent(fullPath)}`
}
