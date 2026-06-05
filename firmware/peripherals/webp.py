"""
WebP encoding via the optional `webp_fast` native module.

`webp_fast` is a MicroPython native module (.mpy) built from libwebp — see
tools/natmod/webp_fast/. Drop webp_fast_xtensawin.mpy into /lib on the device.
If it is missing (or the wrong ABI), available() is False and the camera node
falls back to plain JPEG — no crash.

The camera gives hardware JPEG; the WebP encoder needs pixels, so from_jpeg()
decodes the JPEG (TJpgDec, inside the module) to RGB then encodes WebP. The
JPEG has already filtered sensor noise, so it compresses much smaller than raw
RGB565 would — VGA lands around 11-15 KB at quality 25-40, fitting µReticulum's
16 KB Resource limit.

Knobs:
  quality 0..100  higher = better/larger
  scale   0..3    1/1, 1/2, 1/4, 1/8 downscale during decode (speed/size lever)
  method  0..6    higher = slower/smaller (4 is a good balance; VGA m4 ~7 s)
"""

import sys

webp_fast = None
try:
    if sys.platform == "esp32":
        import webp_fast_xtensawin as webp_fast
    elif sys.platform == "rp2":
        import webp_fast_armv6m as webp_fast
    else:
        import webp_fast
except ImportError:
    try:
        import webp_fast  # arch-agnostic fallback name
    except ImportError:
        webp_fast = None
_OK = webp_fast is not None


def available():
    """True if the native module loaded (correct .mpy present for this firmware)."""
    return _OK


def version():
    """libwebp encoder version int (0xMMmmrr), or 0 if unavailable."""
    return webp_fast.version() if _OK else 0


def from_jpeg(jpeg, quality=30, scale=0, method=4, arena_kb=4096):
    """JPEG bytes -> WebP bytes, or None on failure / module absent."""
    if not _OK:
        return None
    try:
        return webp_fast.from_jpeg(jpeg, quality, scale, method, arena_kb)
    except Exception:
        return None


def from_jpeg_under(jpeg, max_bytes=15500, quality=35, scale=0, method=4,
                    min_quality=10, step=8, arena_kb=4096):
    """JPEG -> WebP guaranteed <= max_bytes (drops quality until it fits).

    Returns the WebP bytes (<= max_bytes if achievable, else the smallest tried),
    or None if encoding fails / module absent. This keeps every reply within the
    16 KB Resource ceiling regardless of scene complexity.
    """
    if not _OK:
        return None
    q = quality
    out = None
    min_q = min(min_quality, quality)   # never step above the requested start (q1 default)
    while q >= min_q:
        out = from_jpeg(jpeg, q, scale, method, arena_kb)
        if out is None:
            return None
        if len(out) <= max_bytes:
            return out
        q -= step
    return out  # smallest we managed (caller may still send or reject)
