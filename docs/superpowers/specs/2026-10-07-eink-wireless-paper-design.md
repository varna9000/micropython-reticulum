# Heltec Wireless Paper e-ink status node — design

Date: 2026-10-07. Branch: `eink-wireless-paper`.

## Goal

Run µReticulum on the Heltec Wireless Paper (ESP32-S3FN8 + SX1262 + 2.13" e-ink)
and show a read-only status screen on the panel: node identity with a QR code of
the LXMF address, and live radio statistics with icons. No keyboard, no input.
The demo is `firmware/example_eink_node.py`; the panel driver and icons are
reusable peripherals.

## Hardware facts (verified on the user's board, Oct 7 2026)

| Item | Value |
|---|---|
| MCU | ESP32-S3FN8, 8 MB embedded flash, **no PSRAM**, 226 KB heap free on MicroPython 1.28 (`ESP32_GENERIC_S3`, plain build) |
| USB | CP2102 UART bridge (`/dev/cu.usbserial-0001`) |
| LoRa pins | SCK 9, MOSI 10, MISO 11, CS 8, BUSY 13, DIO1 14, RST 12, DIO2 RF switch, TCXO 1.8 V (same as `HTIT-WB32LAF` preset) |
| E-ink pins | CS 4, DC 5, RST 6, BUSY 7, SCK 3, MOSI 2 (dedicated bus, no sharing with LoRa) |
| Panel power | **GPIO45, active LOW**. Panel pull-ups on CS/RST appear only with 45 low. Allow ≥300 ms warm-up. |
| Battery | ADC on GPIO20, gated by ADC_CTRL GPIO19 (active LOW). Divider unverified; calibrate against a meter. |
| Button / LED | GPIO0 (boot button), LED GPIO18. Not used by the demo. |
| Panel | Seekink **E0213A367 on SSD1682**, RAM 128×250 portrait, 122 columns visible, 1-bit |
| Chip ID | Command 0x2F then clock 8 bits in on MOSI as input: `(id & 3) == 1` → E0213A367. Board reads 0x01. Other value = LCMEN2R13EFC1 (JD79656), unsupported. |
| BUSY | idle LOW, HIGH while busy |
| Y addressing | **8-bit**: `0x45 start,end` (2 bytes), `0x4F y` (1 byte). The SSD1680 16-bit form collapses the window to one row and renders noise. |
| Refresh | Full (`0x22 0xF7`, `0x20`) 1.4 s with waveform select `0x37 40 80 03 0E`; without 0x37 the refresh is a 510 ms no-op. Fast (`0x22 0xFF`) with border `0x3C 0x81`. Power off `0x22 0x83`, `0x20` 140 ms. `0x10` deep sleep holds BUSY high until HW reset and loses RAM: not used. |

Verified init + full refresh sequence (Heltec `HT_E0213A367`):
HW reset → `0x12` → `0x01 F9 00` → `0x3C 01` → `0x18 80` → `0x37 40 80 03 0E` →
`0x11 03`, `0x44 00 0F`, `0x45 00 F9`, `0x4E 00`, `0x4F 00` → `0x24` + 4000 B →
(`0x26` + 4000 B) → `0x22 F7`, `0x20`, wait BUSY → `0x22 83`, `0x20`.

## Components

### 1. Board preset — `firmware/lora_boards.py`

New entry `heltec_wireless_paper`: the LoRa pins above, `dio2_rf_sw: True`,
`dio3_tcxo_millivolts: 1800`, and a battery block
`{"pin": 20, "divider": <calibrated>, "ctrl_pin": 19, "ctrl_active": 0}`.
The display pins are **not** in the preset (the preset is for radio wiring);
they live in a `EINK` dict in `firmware/config.py` next to `CONFIG`, documented
in the preset's comment.

`peripherals/adc_reader.py` gains optional `ctrl_pin` / `ctrl_active` support in
`init_battery()`: the pin is driven to `ctrl_active` for the duration of a read
and released afterwards. Boards without the keys behave exactly as today.

### 2. Panel driver — `firmware/peripherals/eink_ssd1682.py`

```python
class SSD1682:
    WIDTH, HEIGHT = 250, 122          # landscape, what callers draw on
    def __init__(self, spi, cs, dc, rst, busy, vext=None, vext_active=0, rotate=90)
    fb: framebuf.FrameBuffer          # MONO_HLSB, 250x122, 1 = black ink
    def detect(self) -> int           # bit-banged chip-ID read; raises if not E0213A367
    def flush(self, full=False)       # the only method that touches SPI
    def clear(self)                   # white, full refresh
    def power_off(self)
```

- Constructor drives `vext` active, waits 300 ms, builds the framebuffer and the
  4000-byte portrait transfer buffer. It does **not** touch the panel until the
  first `flush()`; `detect()` is called by the first `flush()` and the result
  cached, so a wrong panel fails loudly with the ID in the message.
- `flush()` transposes the landscape framebuffer into the portrait RAM layout
  (`@micropython.viper` loop over 250×122 pixels; target < 150 ms on the S3),
  writes `0x24` and `0x26`, refreshes, then powers off the booster. The panel
  keeps its image with the booster off.
- Refresh policy inside the driver: the first flush after construction and
  every flush with `full=True` run the full waveform; otherwise fast. A counter
  forces a full refresh after 10 consecutive fast ones (ghosting). Callers never
  choose the waveform except via `full=True`.
- `rotate` is 90 or 270 so the landscape orientation can be flipped once the
  board's USB side is decided on hardware; default chosen on the device.
- Bit polarity: the framebuffer uses 1 = black; the panel uses 1 = white. The
  transpose inverts.
- No framebuffer `scroll`, no partial-window updates (whole-panel RAM writes
  are 4000 bytes at 4 MHz ≈ 8 ms, not worth a window API).
- Errors: BUSY wait has a 10 s timeout that raises `OSError("eink busy
  timeout")` so a dead panel cannot hang the async loop. `flush()` is
  synchronous and blocks the event loop ~1.4 s (full) or ~0.5–1 s (fast); the
  demo rate-limits it.

### 3. Icons and text helpers — `firmware/peripherals/eink_icons.py`

- Hand-drawn 1-bit bitmaps as `bytes` in MONO_HLSB, each with width/height:
  `antenna` (16×16), `battery` (24×12 outline, fill drawn from percent),
  `envelope` (16×12), `lora` (wave arcs, 16×16).
- `rssi_bars(fb, x, y, rssi)`: 5 bars, thresholds −120/−110/−100/−90/−80 dBm.
- `battery(fb, x, y, percent)`: outline bitmap + proportional fill.
- `text2x(fb, s, x, y)`: the built-in 8×8 framebuf font scaled ×2 via a scratch
  `FrameBuffer`, for the headline (node name).
- `qr(fb, x, y, text, scale=2)`: draws `peripherals/qr.py` output. `qr.py` is
  copied verbatim from `reticulum-tdeck/qr.py` (version 8, 49 modules,
  byte-mode ≤192 bytes). At scale 2 the code is 98×98 px.

### 4. Demo application — `firmware/example_eink_node.py`

Based on `example_node.py` (same boot order: imports, `Reticulum`, LXMRouter
setup, `setup_interfaces()`, GC tuning, `rns.run()`), minus serial chat and
WiFi unless `needs_wifi(CONFIG)`.

Screen (250×122 landscape):

```
┌─────────────────────────────────────────────────────┐
│ ▓▓▓▓▓▓▓▓▓▓▓  NodeName (2x)                           │
│ ▓▓ QR 98px ▓▓  ab12cd34  ((•))                         │
│ ▓▓▓▓▓▓▓▓▓▓▓  RX 123   TX 45     ▂▄▆█ -87 dBm          │
│ ▓▓▓▓▓▓▓▓▓▓▓  SNR 9.5  868.8 MHz SF8                  │
│ ▓▓▓▓▓▓▓▓▓▓▓  up 1h02m          [▮▮▮▯] 3.9 V           │
└─────────────────────────────────────────────────────┘
```

- QR content: `lxmf@<32 hex>` (the delivery destination hash), so Sideband and
  MeshChat can scan it to add the node.
- Stats sources: the `LoRaInterface` instance's `rx`, `tx`, `rssi`, `snr`
  attributes and its configured `freq_khz`/`sf`; `time.ticks_ms()` uptime;
  `adc_reader.battery_voltage()` with a simple LiPo 3.3–4.2 V percent map.
- Redraw trigger: an `asyncio` task wakes every 1 s; it redraws when
  `rx`/`tx` changed **or** 30 s passed since the last redraw, but never more
  often than every 5 s. Boot paints once with `full=True`.
- The node registers no LXMF reply logic: incoming LXMF messages only bump an
  "last msg from <name>" line and the envelope icon (optional, drawn when a
  message has been received since boot).
- The panel is not put to deep sleep; `power_off()` after each flush is enough.

### 5. Host tests — `firmware/tests/test_eink_ssd1682.py`, `test_eink_icons.py`

Using the existing `harness.py` shims plus a minimal `framebuf` shim (ported
from `reticulum-tdeck/tests/test_eink_shim.py`) and a fake SPI/Pin recorder:

- init sequence bytes, including `0x37` and the 8-bit `0x45`/`0x4F` forms;
- transpose: a single black pixel at landscape (x, y) lands at the right
  portrait byte/bit for `rotate=90` and `rotate=270`, polarity inverted;
- refresh policy: first flush full, then fast, full again after 10 fast,
  `full=True` resets the counter; power-off sent after every flush;
- chip-ID gate: ID 0x02 raises with the ID in the message;
- icons: `rssi_bars` thresholds, battery fill proportional, QR draws 49×49 at
  the given scale.

Run like the rest of the suite: each file is a standalone script with a
`_run()` collector and `if __name__ == "__main__": sys.exit(...)`, so
`python3 firmware/tests/test_eink_ssd1682.py` exits non-zero on failure.
`harness.py` installs the MicroPython shims.

## Memory budget (no PSRAM)

Framebuffer 32×122 = 3904 B, transfer buffer 4000 B, QR module grid 2401 B,
icon bitmaps < 300 B. Allocated once at boot before the crypto key derivation
fragments the heap, following the project's import-ordering rule.

## Out of scope

Keyboard/button UI, message composing, peer list, partial-window refresh,
LCMEN2R13EFC1 (JD79656) panel support, deep sleep / battery-saving sleep,
porting this board into `reticulum-tdeck`.

## Open items resolved on hardware during implementation

1. `rotate` default (90 vs 270) — pick the one that puts text upright with the
   USB connector on the left.
2. Battery divider value and whether GPIO19 must be low during the read.
3. Transpose timing; if the viper loop is > 150 ms, keep it but document it.
