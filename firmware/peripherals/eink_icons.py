"""1-bit icons and text helpers for the e-ink status screen.

Everything draws into a framebuf.FrameBuffer in MONO_HLSB with 1 = black ink.
Bitmaps are authored as rows of '0'/'1' so they can be read and edited in
place; _bm() packs them once at import. Widths are multiples of 8 so a row is
whole bytes and framebuf can blit them with key=0 (white transparent).
"""
import framebuf


def _bm(rows):
    w = len(rows[0])
    out = bytearray()
    for r in rows:
        v = 0
        for i, ch in enumerate(r):
            if ch == "1":
                v |= 0x80 >> (i & 7)
            if (i & 7) == 7:
                out.append(v)
                v = 0
    return w, len(rows), bytes(out)


ICONS = {
    "antenna": _bm((
        "0000000110000000",
        "0000001111000000",
        "0000011001100000",
        "0000110000110000",
        "0001100110011000",
        "0011001111001100",
        "0010011001100100",
        "0000110110110000",
        "0000000110000000",
        "0000000110000000",
        "0000000110000000",
        "0000000110000000",
        "0000000110000000",
        "0000000110000000",
        "0000011111100000",
        "0001111111111000",
    )),
    "envelope": _bm((
        "1111111111111111",
        "1000000000000001",
        "1100000000000011",
        "1010000000000101",
        "1001000000001001",
        "1000100000010001",
        "1000010000100001",
        "1000001001000001",
        "1000000110000001",
        "1000000000000001",
        "1000000000000001",
        "1111111111111111",
    )),
    "lora": _bm((
        "0100000000000010",
        "1010000000000101",
        "1001000000001001",
        "1000100110010001",
        "1000101001010001",
        "1000101111010001",
        "1000101001010001",
        "1000100110010001",
        "1001000000001001",
        "1010000000000101",
        "0100000000000010",
    )),
}

BARS_W = 24
BARS_H = 12
_RSSI_STEPS = (-120, -110, -100, -90, -80)      # dBm, bar n lit when rssi >= step n


def icon(fb, name, x, y):
    w, h, bits = ICONS[name]
    src = framebuf.FrameBuffer(bytearray(bits), w, h, framebuf.MONO_HLSB)
    fb.blit(src, x, y, 0)


def rssi_bars(fb, x, y, rssi):
    """Five bars of growing height; returns how many are lit. None -> 0."""
    lit = 0
    if rssi is not None:
        for thr in _RSSI_STEPS:
            if rssi >= thr:
                lit += 1
    for n in range(5):
        bh = 4 + 2 * n                       # 4,6,8,10,12
        bx = x + n * 5                       # 3 px bar + 2 px gap -> 24 px total
        by = y + BARS_H - bh
        if n < lit:
            fb.fill_rect(bx, by, 3, bh, 1)
        else:
            fb.hline(bx, y + BARS_H - 1, 3, 1)   # footprint only
    return lit


def battery(fb, x, y, percent):
    """24x12 gauge: outline + nub, fill proportional to percent (None = empty)."""
    fb.rect(x, y, 21, 12, 1)
    fb.fill_rect(x + 21, y + 3, 3, 6, 1)
    if percent is None:
        return
    p = max(0, min(100, int(percent)))
    w = (17 * p + 50) // 100
    if w:
        fb.fill_rect(x + 2, y + 2, w, 8, 1)


def text2x(fb, s, x, y):
    """Built-in 8x8 font scaled 2x via a scratch buffer (headline only)."""
    w = 8 * len(s)
    scratch = framebuf.FrameBuffer(bytearray(((w + 7) // 8) * 8), w, 8, framebuf.MONO_HLSB)
    scratch.text(s, 0, 0, 1)
    for yy in range(8):
        for xx in range(w):
            if scratch.pixel(xx, yy):
                fb.fill_rect(x + 2 * xx, y + 2 * yy, 2, 2, 1)


def qr_code(fb, x, y, text, scale=2):
    """Draw qr.encode(text) with `scale` px per module; returns the side length."""
    from . import qr
    mods = qr.encode(text)
    n = qr.SIZE
    for my in range(n):
        row = my * n
        for mx in range(n):
            if mods[row + mx]:
                fb.fill_rect(x + mx * scale, y + my * scale, scale, scale, 1)
    return n * scale


def qr_framebuffer(text, scale=2):
    """Encode once into its own FrameBuffer (side = 49*scale px) for blitting.

    qr.encode() needs several KB of transient heap (module grid, codewords,
    GF tables); on a PSRAM-less board that is not available once the stack is
    running, so callers encode at boot and keep this small buffer instead."""
    from . import qr
    side = qr.SIZE * scale
    buf = bytearray(((side + 7) // 8) * side)
    fb = framebuf.FrameBuffer(buf, side, side, framebuf.MONO_HLSB)
    qr_code(fb, 0, 0, text, scale)
    return fb
