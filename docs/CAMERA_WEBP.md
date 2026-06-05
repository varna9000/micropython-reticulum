# Camera image settings (JPEG & WebP)

The camera node ([`example_camera_node.py`](../firmware/example_camera_node.py)) replies to
any LXMF message with a photo. By default it sends a **640×480 (VGA) WebP** that fits
µReticulum's 16 KB LoRa transfer limit. Every setting below can be changed **at runtime**
by messaging the node — no re-flash needed.

## How it works (and why WebP)

LoRa links are slow and µReticulum caps a single image transfer (a *Resource*) at **16 KB**.
A VGA JPEG at usable quality is larger than that — but **WebP** compresses the same picture
much smaller, so a 640×480 photo fits the budget with detail to spare.

The camera only produces hardware **JPEG**, and the WebP encoder needs raw pixels, so the
pipeline is:

```
sensor → hardware JPEG (clean, q90) → decode → WebP (small) → LXMF ["webp", data]
```

Re-using the JPEG as the source is deliberate: the JPEG step **filters sensor noise**, and
that is exactly what lets WebP shrink a VGA frame to ~7 KB. (Raw RGB565 keeps the noise and
compresses far worse.) The WebP encoder is the optional native module `webp_fast`
(`/lib/webp_fast_xtensawin.mpy`). If it is missing, the node automatically **falls back to
sending JPEG** — nothing breaks.

## Changing settings from MeshChat / Sideband

Send the camera node a text message:

| message | does |
|---|---|
| `image` | capture a photo and send it back |
| `help` | list every keyword |
| `settings` | show the current values |
| `<key>` | read one value, e.g. `webp_quality` |
| `<key> <value>` | set it, e.g. `webp_quality 5` |

`set webp_quality 5` and `webp_quality=5` also work. A change applies on the **next** `image`
request. Mistype a keyword and the node replies with a "did you mean…?" hint.

## The settings at a glance

| keyword | range | default | what it controls |
|---|---|---|---|
| `resolution` | qqvga … uxga | `vga` (640×480) | frame size |
| `format` | `webp` / `jpg` | `webp` | WebP pipeline, or raw JPEG |
| `quality` | 0–100 | `90` | **JPEG capture** (source) quality |
| `webp_quality` | 0–100 | `1` | **WebP output** quality → file size |
| `webp_scale` | 0–3 | `0` | downscale 1/1, 1/2, 1/4, 1/8 |
| `webp_method` | 0–6 | `4` | encoder effort (size vs speed) |
| `flash` | `off`/`on`/`auto` | `off` | onboard NeoPixel fill flash (fire when dark in `auto`) |
| `flash_threshold` | 0–255 | `50` | `auto` flash fires when a brightness probe reads below this |
| `night` | on/off | `off` | night mode: long exposure + gain + flash for near-dark |
| `exposure` | `auto` / 0–1200 | `auto` | auto-exposure, or a fixed exposure time |
| `ae_level` | −2 … +2 | `0` | auto-exposure brightness bias |
| `warmup` | n | `12` | frames discarded so exposure settles |
| `vflip` | on/off | `on` | vertical flip |
| `hmirror` | on/off | `on` | horizontal mirror |

## The WebP parameters in detail

### Two "quality" knobs — don't confuse them

- **`quality`** is the **JPEG capture** quality (the *source*). High (90) = clean, noise-free
  pixels. It controls **cleanliness, not output size** — a clean source just lets WebP do its
  job. Leave it at 90. (Needs `fb_count=2`, set by default, so the big q90 frame doesn't
  overflow the camera buffer → `cam_hal: FB-OVF`.)
- **`webp_quality`** is the **WebP output** compression. This is the knob that actually **sets
  the file size**.

A useful mental model: the *capture* `quality` sets how good the pixels are; `webp_quality`
sets how many bytes you spend describing them.

### `webp_quality` — the size dial (0–100)

Lower = smaller and softer; higher = bigger and crisper. Measured on a clean q90 VGA source
(640×480, `webp_method=4`):

| `webp_quality` | size | look |
|---|---|---|
| 1 | ~7 KB | soft but clearly recognizable |
| 5 | ~9 KB | good, slight softening |
| 10 | ~11 KB | crisp |
| 20 | ~14 KB | crisper (near the limit) |
| 30 | ~17 KB | ❌ over the 16 KB limit |

The node also **auto-lowers** `webp_quality` if a busy scene would exceed `CAM_MAX_BYTES`
(15.5 KB), so a reply always fits the budget regardless of scene complexity.

> Note: the *output* size depends on `webp_quality`, **not** on the source `quality`. A q90
> source and a q40 source produce roughly the same WebP size at a given `webp_quality` — the
> q90 source just looks cleaner.

### `webp_scale` — downscale during decode (0–3)

| value | output | notes |
|---|---|---|
| 0 | 640×480 (1/1) | full VGA (default) |
| 1 | 320×240 (1/2) | ~½ the size, ~3× faster, averages out noise |
| 2 | 160×120 (1/4) | small thumbnail |
| 3 | 80×60 (1/8) | tiny |

Downscaling shrinks the file, **speeds up the encode** (QVGA ≈ 2 s vs VGA ≈ 7 s), and reduces
noise — at the cost of resolution. Default `0` keeps full VGA detail.

### `webp_method` — encoder effort (0–6)

How hard the encoder works to compress. Higher = **smaller file at the same quality**, but
slower.

- `0` — fastest, but ~3× larger files. **Don't use it for sending.**
- `4` — good balance (≈7 s at VGA). **Default.**
- `6` — ~5 % smaller than 4, ~1 s slower.

### `format` — `webp` or `jpg`

- `webp` (default) — the full pipeline above; best quality-per-byte.
- `jpg` — send the raw camera JPEG with **no** re-encoding. Instant (no ~7 s encode), but
  larger and lower quality-per-byte at VGA. Also the automatic fallback if `webp_fast.mpy`
  is absent.

## Exposure & orientation (camera, not WebP)

- **`exposure`** — `auto` (default) lets the sensor auto-expose; an integer (~0–1200) fixes the
  exposure time (higher = brighter/longer).
- **`ae_level`** — biases the auto-exposure target brightness, `−2 … +2` (negative = darker).
  Use it if auto images come out over- or under-exposed.
- **`warmup`** — frames discarded before the kept one so auto-exposure/gain/white-balance can
  converge. Too few → over/under-exposed; 10–12 is usually enough.
- **`vflip` / `hmirror`** — flip/mirror, depending on how the board is mounted.

## Flash & night mode (low light)

The board's **onboard NeoPixel (GPIO48)** doubles as a fill flash:
- **`flash off`** (default) — never.
- **`flash on`** — always fire for the shot.
- **`flash auto`** — a quick grayscale brightness probe runs first; the flash fires only
  if the scene reads below `flash_threshold` (adds ~0.7 s/capture). Useful for dim scenes.

The LED is colour-corrected to a neutral white (`(255,150,210)` — WS2812 green is
over-bright, so plain white looks green). It's a small LED: it lights a **near/centered
subject**, not a whole room.

**`night on`** is a best-effort mode for near-dark scenes. It drops the camera clock
(longer exposure), raises the gain ceiling, and forces the flash. The result is **dim,
grainy and slow** — the most this hardware (an OV2640 + a tiny LED) can do in near-black.
For genuine dark capture you need a real high-power LED/flash on a spare GPIO. In normal
or dim light, leave `night off`.

## Timing & limits

- A **VGA WebP encode is synchronous (~7 s at method 4)** — it briefly blocks the async event
  loop, *before* the Resource transfer starts. `webp_scale 1` (~2 s) or a lower `webp_method`
  are the speed levers.
- Replies are kept under **16 KB** (`CAM_MAX_BYTES = 15500`); `webp_quality` is auto-lowered
  if a scene would exceed it.

## Requirements

- **Camera-enabled MicroPython** (cnadler86 build) — needed for `import camera`.
- **`webp_fast.mpy`** in `/lib` (the native WebP encoder) — otherwise the node sends JPEG.
  Built from [`tools/natmod/webp_fast/`](../tools/natmod/webp_fast/); must match the firmware's
  MicroPython version.
- `fb_count = 2` (default) so a high-quality (q90) JPEG doesn't overflow the frame buffer.

## Defaults

```
resolution = vga      format = webp        quality = 90 (JPEG source)
webp_quality = 1 (~7 KB)   webp_scale = 0   webp_method = 4
ae_level = 0          warmup = 12          fb_count = 2
```

## Rules of thumb

- **Smaller / faster transfers** → lower `webp_quality` (1–5), or `webp_scale 1`.
- **Crisper image** → raise `webp_quality` (10–20).
- Leave `quality` at 90 and `webp_method` at 4 unless you have a reason to change them.
