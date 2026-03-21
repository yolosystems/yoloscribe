"""Rolling per-channel request counter for high-volume detection.

Uses a sliding window (10 minutes) to track how many requests each channel
has sent. When a channel crosses the threshold for the first time in a window,
`record_request` returns True so the caller can post a one-time warning.

State is in-process memory — resets on bot restart, which is fine for a
best-effort advisory warning.
"""

import time
from collections import defaultdict

_WINDOW_SECONDS: int = 600   # 10-minute rolling window
HIGH_VOLUME_THRESHOLD: int = 50  # requests in the window before warning fires

# channel_id → list of monotonic timestamps within the current window
_timestamps: dict[str, list[float]] = defaultdict(list)


def record_request(channel_id: str) -> bool:
    """Record a request for this channel.

    Prunes timestamps outside the rolling window, appends the current time,
    then returns True if the channel just crossed HIGH_VOLUME_THRESHOLD for
    the first time in this window (i.e. count went from threshold to threshold+1).
    """
    now = time.monotonic()
    cutoff = now - _WINDOW_SECONDS
    ts = [t for t in _timestamps[channel_id] if t >= cutoff]
    ts.append(now)
    _timestamps[channel_id] = ts
    # Fire warning exactly when count crosses the threshold (not on every subsequent call)
    return len(ts) == HIGH_VOLUME_THRESHOLD + 1


def reset(channel_id: str | None = None) -> None:
    """Clear counters. Passing None clears all channels (test helper)."""
    if channel_id is None:
        _timestamps.clear()
    else:
        _timestamps.pop(channel_id, None)
