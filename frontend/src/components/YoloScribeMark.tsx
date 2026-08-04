/**
 * YoloScribe brand mark — a fountain-pen nib (the "scribe") with an orange AI
 * spark at its tip, on the yolo navy→orange gradient tile. Self-contained SVG,
 * safe to render anywhere (no external assets, no theme dependency).
 */
export default function YoloScribeMark({ size = 64, rounded = true }: { size?: number; rounded?: boolean }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 128 128"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      role="img"
      aria-label="YoloScribe"
    >
      <defs>
        <linearGradient id="ys-tile" x1="10" y1="8" x2="118" y2="122" gradientUnits="userSpaceOnUse">
          <stop stopColor="#2a1f6e" />
          <stop offset="0.52" stopColor="#0c0c1e" />
          <stop offset="1" stopColor="#ff4500" />
        </linearGradient>
        <linearGradient id="ys-nib" x1="64" y1="26" x2="64" y2="104" gradientUnits="userSpaceOnUse">
          <stop stopColor="#f7f3ff" />
          <stop offset="1" stopColor="#c9bcff" />
        </linearGradient>
        <radialGradient id="ys-spark" cx="0.5" cy="0.5" r="0.5">
          <stop stopColor="#ffb08a" />
          <stop offset="0.45" stopColor="#ff4500" />
          <stop offset="1" stopColor="#ff4500" stopOpacity="0" />
        </radialGradient>
      </defs>

      {/* gradient tile */}
      <rect x="4" y="4" width="120" height="120" rx={rounded ? 30 : 0} fill="url(#ys-tile)" />
      <rect x="4.5" y="4.5" width="119" height="119" rx={rounded ? 29.5 : 0} stroke="#ffffff" strokeOpacity="0.10" />

      {/* soft glow behind the spark */}
      <circle cx="64" cy="97" r="26" fill="url(#ys-spark)" opacity="0.65" />

      {/* fountain-pen nib with curved shoulders */}
      <path
        d="M64 27 C74 27 89 58 89 77 L64 99 L39 77 C39 58 54 27 64 27 Z"
        fill="url(#ys-nib)"
      />
      {/* nib slit */}
      <path d="M64 55 L64 89" stroke="#0c0c1e" strokeWidth="4.5" strokeLinecap="round" />
      {/* breather hole */}
      <circle cx="64" cy="52" r="4.5" fill="#0c0c1e" />

      {/* AI spark at the tip */}
      <circle cx="64" cy="99" r="8" fill="#ff4500" />
      <circle cx="64" cy="99" r="4" fill="#ffd9c2" />

      {/* small sparkle accent, top-right */}
      <path d="M98 34 l2.6 6.4 6.4 2.6 -6.4 2.6 -2.6 6.4 -2.6 -6.4 -6.4 -2.6 6.4 -2.6 z" fill="#ff8f5e" />
    </svg>
  )
}
