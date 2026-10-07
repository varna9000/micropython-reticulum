# Heltec Wireless Paper E-Ink Status Node Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run µReticulum on the Heltec Wireless Paper and paint a read-only status screen (QR contact code + live LoRa statistics with icons) on its 2.13" SSD1682 e-ink panel.

**Architecture:** A pure-MicroPython panel driver (`peripherals/eink_ssd1682.py`) owns a 250×122 landscape 1-bit framebuffer and is the only code that talks SPI; it transposes to the panel's 128×250 portrait RAM on `flush()` and manages full/fast waveforms. A status renderer (`peripherals/eink_status.py`) draws icons and text from a plain stats dict, so it is testable on the host. The demo app (`example_eink_node.py`) boots the normal LXMF node exactly like `example_node.py` and runs one extra asyncio task that gathers stats and flushes the panel on change or every 30 s.

**Tech Stack:** MicroPython 1.28 (`ESP32_GENERIC_S3`, no PSRAM), `framebuf`, `machine.SPI/Pin/ADC`, `uasyncio`, `micropython.viper` for the transpose; host tests are standalone CPython scripts using `firmware/tests/harness.py` shims.

**Spec:** `docs/superpowers/specs/2026-10-07-eink-wireless-paper-design.md`

## Global Constraints

- MicroPython-only, zero external dependencies, no C extensions (CLAUDE.md). `micropython.viper` is allowed (it is bytecode, not C).
- No PSRAM on this board: 226 KB free at boot. All display buffers (3904 B framebuffer, 4000 B transfer buffer, 2401 B QR grid) are allocated once in the driver/renderer constructors, before `Reticulum()` runs key derivation.
- E-ink pins: CS 4, DC 5, RST 6, BUSY 7, SCK 3, MOSI 2. Panel power GPIO45 **active LOW**, ≥300 ms warm-up before the first command.
- SSD1682 **8-bit Y addressing**: `0x45 start,end` (2 bytes), `0x4F y` (1 byte). Never the 4/2-byte SSD1680 form.
- Waveform select `0x37 40 80 03 0E` must precede the first refresh; full refresh = `0x22 F7`, fast = `0x22 FF`; border `0x3C 01` for full, `0x3C 81` for fast; power off = `0x22 83`. BUSY idle LOW, HIGH while busy. Never send `0x10` (deep sleep).
- Chip ID: bit-bang `0x2F`, clock 8 bits in on MOSI as input; `(id & 3) == 1` is the only supported panel.
- Framebuffer convention: 1 = black ink. Panel RAM convention: 1 = white. Transpose inverts.
- Host tests are standalone scripts with the `_run()` collector from `firmware/tests/test_lora_split.py`; run `python3 firmware/tests/<file>.py`, exit code non-zero on failure. No pytest.
- Board preset holds radio pins only; display pins live in `config.EINK`.
- Commit after every task; commit messages in the repo's `area: imperative summary` style.

## Review Focus

1. **Panel absent or dead at boot** (BUSY stuck HIGH): `flush()` must raise `OSError("eink busy timeout")` after 10 s instead of hanging the asyncio loop forever. Test in Task 4.
2. **Node name longer than 8 characters**: the 2× headline must truncate, not draw off the right edge or wrap onto the QR. Test in Task 6.
3. **RSSI/SNR unknown before the first packet** (`LoRaInterface.rssi` does not exist until a frame arrives): the renderer must draw `--` and zero bars, not crash on `None`. Test in Task 6.
4. **Battery not configured** (`adc_reader.battery_voltage()` returns `None`): the renderer must draw the icon empty and `--V`, not `TypeError`. Test in Task 6.
5. **Reboot with the panel already showing an image**: the first flush after construction must be a full refresh (fast waveform on unknown RAM ghosts badly). Test in Task 4.

---

## File Structure

| Path | Responsibility |
|---|---|
| `firmware/lora_boards.py` (modify) | `heltec_wireless_paper` radio preset + battery block with `ctrl_pin` |
| `firmware/peripherals/adc_reader.py` (modify) | Optional ADC enable pin (`ctrl_pin`, `ctrl_active`) driven only during a read |
| `firmware/peripherals/eink_ssd1682.py` (create) | SSD1682 driver: power, chip-ID gate, init, transpose, full/fast flush, power-off |
| `firmware/peripherals/qr.py` (create, verbatim copy) | Version-8 QR encoder from `reticulum-tdeck/qr.py` |
| `firmware/peripherals/eink_icons.py` (create) | 1-bit icon bitmaps, RSSI bars, battery gauge, 2× text, QR blit |
| `firmware/peripherals/eink_status.py` (create) | `render(fb, stats)` status screen layout + `format_uptime`, `battery_percent` |
| `firmware/config.py` (modify) | `EINK` pin dict; commented `"board": "heltec_wireless_paper"` hint |
| `firmware/example_eink_node.py` (create) | Demo node: boots like `example_node.py`, adds the display task |
| `firmware/tests/eink_fakes.py` (create) | Host shims: `framebuf`, fake `machine.Pin/SPI/ADC`, fake `time.ticks_*`, SPI command parser |
| `firmware/tests/test_adc_ctrl.py` (create) | ctrl_pin behaviour |
| `firmware/tests/test_eink_ssd1682.py` (create) | Driver tests |
| `firmware/tests/test_eink_icons.py` (create) | Icons + renderer tests |
| `README.md` (modify) | Short "Heltec Wireless Paper" section |

---

### Task 1: Board preset and battery enable pin

**Files:**
- Modify: `firmware/lora_boards.py` (append a preset before the closing `}` of `LORA_BOARDS`, currently line ~166)
- Modify: `firmware/peripherals/adc_reader.py:20-52`
- Test: `firmware/tests/test_adc_ctrl.py`

**Interfaces:**
- Consumes: `lora_boards.battery_config(config)` (exists), `machine.ADC`, `machine.Pin`.
- Produces: `adc_reader.init(adc_map, dividers=None, atten="11db", ctrl=None)` where `ctrl = {"battery": (pin_num, active_level)}`; `adc_reader.init_battery(config)` reads `ctrl_pin`/`ctrl_active` from the battery block; `adc_reader.read_voltage(name)` drives the ctrl pin active for the read and releases it (sets the inactive level) afterwards. Preset name `"heltec_wireless_paper"`.

- [ ] **Step 1: Write the failing test**

Create `firmware/tests/test_adc_ctrl.py`:

```python
"""adc_reader: optional ADC enable (ctrl) pin, driven only while reading."""
import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401  (installs micropython/machine shims)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- fake machine.ADC / machine.Pin -----------------------------------------
_log = []


class _Pin:
    OUT = 1
    IN = 0
    created = {}

    def __init__(self, n, mode=None, pull=None, value=None):
        self.n = n
        self.v = 0 if value is None else value
        _Pin.created[n] = self
        if value is not None:
            _log.append(("pin", n, value))

    def value(self, v=None):
        if v is None:
            return self.v
        self.v = v
        _log.append(("pin", self.n, v))


class _ADC:
    ATTN_0DB = 0
    ATTN_2_5DB = 1
    ATTN_6DB = 2
    ATTN_11DB = 3

    def __init__(self, pin):
        self.pin = pin

    def atten(self, a):
        pass

    def read_u16(self):
        _log.append(("read", self.pin.n))
        return 32768          # 1.65 V at the pin


machine = sys.modules["machine"]
machine.Pin = _Pin
machine.ADC = _ADC

import peripherals.adc_reader as adc_reader  # noqa: E402


def _reset():
    adc_reader.channels.clear()
    adc_reader._ctrl.clear()
    _log.clear()
    _Pin.created.clear()


def test_no_ctrl_pin_behaves_as_before():
    _reset()
    adc_reader.init({"battery": 20}, dividers={"battery": 2.0})
    v = adc_reader.read_voltage("battery")
    assert abs(v - 3.3) < 0.01, v
    assert not any(e[0] == "pin" for e in _log), _log


def test_ctrl_pin_driven_active_during_read_then_released():
    _reset()
    adc_reader.init({"battery": 20}, dividers={"battery": 2.0},
                    ctrl={"battery": (19, 0)})
    # construction parks the pin at the INACTIVE level (1 for active-low)
    assert ("pin", 19, 1) in _log, _log
    _log.clear()
    adc_reader.read_voltage("battery")
    kinds = [e for e in _log if e[0] in ("pin", "read")]
    assert kinds[0] == ("pin", 19, 0), kinds          # enabled first
    assert all(e[0] == "read" for e in kinds[1:-1]), kinds
    assert kinds[-1] == ("pin", 19, 1), kinds         # released last


def test_init_battery_reads_ctrl_from_board_block():
    _reset()
    config = {"interfaces": [{"board": "heltec_wireless_paper"}],
              "lora_boards": {"heltec_wireless_paper": {
                  "battery": {"pin": 20, "divider": 2.0,
                              "ctrl_pin": 19, "ctrl_active": 0}}}}
    bat = adc_reader.init_battery(config)
    assert bat["ctrl_pin"] == 19
    gate_pin, active = adc_reader._ctrl["battery"]     # (Pin object, level)
    assert gate_pin.n == 19 and active == 0


def test_preset_exists_with_radio_pins():
    from lora_boards import LORA_BOARDS
    p = LORA_BOARDS["heltec_wireless_paper"]
    assert (p["sck_pin"], p["mosi_pin"], p["miso_pin"], p["cs_pin"],
            p["busy_pin"], p["dio1_pin"], p["reset_pin"]) == (9, 10, 11, 8, 13, 14, 12)
    assert p["dio2_rf_sw"] is True and p["dio3_tcxo_millivolts"] == 1800
    assert p["battery"]["pin"] == 20 and p["battery"]["ctrl_pin"] == 19
    assert p["battery"]["ctrl_active"] == 0


def _run():
    import traceback
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except Exception as e:
            failed += 1
            print("FAIL  " + name + "  ->  " + repr(e))
            traceback.print_exc()
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 firmware/tests/test_adc_ctrl.py`
Expected: FAIL on `_ctrl` attribute missing / `ctrl` keyword / `KeyError: 'heltec_wireless_paper'`.

- [ ] **Step 3: Add the preset**

In `firmware/lora_boards.py`, insert before the final `}` that closes `LORA_BOARDS` (after the `"HTIT-WB32LAF"` block):

```python
    # Heltec Wireless Paper (ESP32-S3FN8, no PSRAM, SX1262, 2.13" e-ink).
    # Also sold unbranded as "2.13 inch LoRa E-Ink ESP32-S3" (Amazon B0DBHG2X3D).
    # Radio wiring is the standard Heltec S3 map (same as HTIT-WB32LAF).
    #
    # The e-ink panel is NOT part of this preset (presets are radio wiring);
    # its pins live in config.EINK and are consumed by example_eink_node.py:
    #   CS 4, DC 5, RST 6, BUSY 7, SCK 3, MOSI 2, VEXT GPIO45 (active LOW).
    #
    # Battery: ADC on GPIO20 behind an enable transistor on GPIO19 (active LOW,
    # Meshtastic ADC_CTRL). The divider is nominally 1:1 (vbat = 2 * vpin);
    # calibrate against a meter and adjust "divider" here.
    "heltec_wireless_paper": {
        "spi_bus": 1,
        "sck_pin": 9,
        "mosi_pin": 10,
        "miso_pin": 11,
        "cs_pin": 8,
        "busy_pin": 13,
        "dio1_pin": 14,
        "reset_pin": 12,
        "dio2_rf_sw": True,
        "dio3_tcxo_millivolts": 1800,
        "battery": {"pin": 20, "divider": 2.0, "ctrl_pin": 19, "ctrl_active": 0},
    },

```

- [ ] **Step 4: Add ctrl-pin support to adc_reader**

Replace the top of `firmware/peripherals/adc_reader.py` from `channels = {}` through the end of `read_voltage()` with:

```python
channels = {}          # name -> (ADC, divider)
_ctrl = {}             # name -> (Pin, active_level) — ADC enable gate, optional
_SAMPLES = 8           # averaged per read; the ESP32-S3 ADC is noisy


def init(adc_map, dividers=None, atten="11db", ctrl=None):
    """adc_map = {"battery": 1, "light": 2}; dividers = {"battery": 2.0};
    ctrl = {"battery": (19, 0)} -> GPIO19 must be driven to 0 while reading.

    Each channel stores (ADC, divider); divider defaults to 1.0 (direct connect).
    A ctrl pin is parked at its INACTIVE level between reads so the divider
    does not drain the battery (Heltec boards gate the divider with a FET).
    """
    from machine import ADC, Pin
    dividers = dividers or {}
    ctrl = ctrl or {}
    for name, pin_num in adc_map.items():
        adc = ADC(Pin(pin_num))
        try:
            _attn = {"0db": ADC.ATTN_0DB, "2_5db": ADC.ATTN_2_5DB,
                     "6db": ADC.ATTN_6DB, "11db": ADC.ATTN_11DB}
            adc.atten(_attn.get(atten, ADC.ATTN_11DB))
        except AttributeError:
            pass   # ports without configurable attenuation (e.g. RP2040)
        channels[name] = (adc, float(dividers.get(name, 1.0)))
        if name in ctrl:
            cpin, active = ctrl[name]
            _ctrl[name] = (Pin(cpin, Pin.OUT, value=1 - active), active)


def init_battery(config):
    """Configure the 'battery' channel from the active board's preset
    (lora_boards.battery_config) — the board owns the pin + divider. NO-OP when
    the board declares no battery (e.g. stock XIAO ESP32-S3), so callers can call
    this unconditionally and just list this module as a peripheral; process() and
    battery_voltage() return None when nothing is wired. Returns the params/None."""
    from lora_boards import battery_config
    bat = battery_config(config)
    if bat:
        ctrl = None
        if "ctrl_pin" in bat:
            ctrl = {"battery": (bat["ctrl_pin"], bat.get("ctrl_active", 0))}
        init({"battery": bat["pin"]}, dividers={"battery": bat.get("divider", 1.0)},
             ctrl=ctrl)
    return bat


def _raw(adc, name=None):
    gate = _ctrl.get(name)
    if gate:
        gate[0].value(gate[1])
    s = 0
    for _ in range(_SAMPLES):
        s += adc.read_u16()
    if gate:
        gate[0].value(1 - gate[1])
    return s // _SAMPLES


def read_voltage(name):
    """Scaled voltage (float) for one channel, or None if not configured.

    Handy for code that wants the number rather than the formatted string
    (e.g. the transport router's web dashboard battery gauge)."""
    ch = channels.get(name)
    if not ch:
        return None
    adc, divider = ch
    return _raw(adc, name) * 3.3 / 65535 * divider
```

Then in `_read(name, adc, divider)` change `raw = _raw(adc)` to `raw = _raw(adc, name)`.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python3 firmware/tests/test_adc_ctrl.py`
Expected: `4/4 passed`.

Also run the existing suite that touches boards to be sure nothing broke:
`for t in firmware/tests/test_*.py; do python3 "$t" >/dev/null || echo "FAILED $t"; done`
Expected: no `FAILED` lines.

- [ ] **Step 6: Commit**

```bash
git add firmware/lora_boards.py firmware/peripherals/adc_reader.py firmware/tests/test_adc_ctrl.py
git commit -m "boards: heltec_wireless_paper preset; adc_reader: optional ADC enable pin"
```

---

### Task 2: Host test fakes for the display stack

**Files:**
- Create: `firmware/tests/eink_fakes.py`

**Interfaces:**
- Produces (imported by Tasks 3–6 tests): `eink_fakes.install()` → installs `framebuf` shim, fake `machine.Pin/SPI`, fake `time.ticks_ms/ticks_diff/sleep_ms/sleep_us`; `eink_fakes.pins` dict `n -> FakePin`; `eink_fakes.log` list of events `("pin", n, v)` / `("spi", bytes)`; `eink_fakes.clock_ms` int; `eink_fakes.reset()`; `eink_fakes.commands(dc=5)` → list of `(cmd_byte, data_bytes)` parsed from `log`; `FakePin.in_bits` (list popped on reads while mode is IN), `FakePin.stuck` (constant read value or None).
- `framebuf` shim: `FrameBuffer(buf, w, h, fmt)` with `pixel, fill, fill_rect, rect, hline, vline, text, blit`, `MONO_HLSB = 3`, stride `(w + 7) // 8`.

- [ ] **Step 1: Write the fakes module**

```python
"""Host shims for the e-ink stack: framebuf, machine.Pin/SPI, time.ticks_*.

Import harness first (it creates the empty `machine` module), then call
install(). Everything the driver does is recorded in `log`, in order:
    ("pin", gpio, value)   a Pin output changed
    ("spi", bytes)         one SPI write
commands(dc) folds the log into [(cmd, data_bytes), ...] by tracking the DC pin.
"""
import sys
import types
import time

pins = {}
log = []
clock_ms = 0


class FakePin:
    OUT = 1
    IN = 0
    PULL_UP = 1
    PULL_DOWN = 2

    def __init__(self, n, mode=None, pull=None, value=None):
        self.n = n
        self.mode = mode
        self.in_bits = []        # consumed on reads while mode == IN
        self.stuck = None        # constant read value, overrides everything
        self._v = 0
        pins[n] = self
        if value is not None:
            self.value(value)

    def init(self, mode=None, pull=None, value=None):
        self.mode = mode
        if value is not None:
            self.value(value)

    def value(self, v=None):
        if v is None:
            if self.stuck is not None:
                return self.stuck
            if self.in_bits:
                return self.in_bits.pop(0)
            return self._v
        self._v = v
        log.append(("pin", self.n, v))

    def __call__(self, v=None):
        return self.value(v)


def Pin(n, mode=None, pull=None, value=None):
    """Return the one FakePin per GPIO so tests can poke it before the driver runs."""
    p = pins.get(n)
    if p is None:
        return FakePin(n, mode, pull, value)
    p.init(mode, pull, value)
    return p


Pin.OUT = FakePin.OUT
Pin.IN = FakePin.IN
Pin.PULL_UP = FakePin.PULL_UP
Pin.PULL_DOWN = FakePin.PULL_DOWN


class FakeSPI:
    def __init__(self, bus, baudrate=0, polarity=0, phase=0, sck=None, mosi=None, miso=None):
        self.bus = bus
        self.baudrate = baudrate
        self.deinit_called = 0

    def write(self, buf):
        log.append(("spi", bytes(buf)))

    def deinit(self):
        self.deinit_called += 1


class _FrameBuffer:
    """MONO_HLSB subset of framebuf.FrameBuffer with the real stride rule."""

    def __init__(self, buf, width, height, fmt):
        self.buf = buf
        self.width = width
        self.height = height
        self.stride = (width + 7) // 8

    def pixel(self, x, y, c=None):
        if not (0 <= x < self.width and 0 <= y < self.height):
            return 0
        i = y * self.stride + (x >> 3)
        bit = 7 - (x & 7)
        if c is None:
            return (self.buf[i] >> bit) & 1
        if c:
            self.buf[i] |= 1 << bit
        else:
            self.buf[i] &= ~(1 << bit) & 0xFF

    def fill(self, c):
        v = 0xFF if c else 0x00
        for i in range(len(self.buf)):
            self.buf[i] = v

    def fill_rect(self, x, y, w, h, c):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.pixel(xx, yy, c)

    def hline(self, x, y, w, c):
        self.fill_rect(x, y, w, 1, c)

    def vline(self, x, y, h, c):
        self.fill_rect(x, y, 1, h, c)

    def rect(self, x, y, w, h, c, f=False):
        if f:
            self.fill_rect(x, y, w, h, c)
            return
        self.hline(x, y, w, c)
        self.hline(x, y + h - 1, w, c)
        self.vline(x, y, h, c)
        self.vline(x + w - 1, y, h, c)

    def text(self, s, x, y, c=1):
        """Deterministic stand-in for the built-in 8x8 font: glyph bit (col,row)
        is set when ((ord(ch) + row) >> col) & 1. Enough to test placement/scaling."""
        for k, ch in enumerate(s):
            o = ord(ch)
            for row in range(8):
                for col in range(8):
                    if ((o + row) >> col) & 1:
                        self.pixel(x + k * 8 + col, y + row, c)

    def blit(self, src, x, y, key=-1):
        for yy in range(src.height):
            for xx in range(src.width):
                v = src.pixel(xx, yy)
                if key >= 0 and v == key:
                    continue
                self.pixel(x + xx, y + yy, v)


def _ticks_ms():
    return clock_ms


def _ticks_diff(a, b):
    return a - b


def _sleep_ms(n):
    global clock_ms
    clock_ms += n


def _sleep_us(n):
    global clock_ms
    clock_ms += max(1, n // 1000)


def install():
    fb = types.ModuleType("framebuf")
    fb.FrameBuffer = _FrameBuffer
    fb.MONO_HLSB = 3
    sys.modules["framebuf"] = fb
    m = sys.modules.setdefault("machine", types.ModuleType("machine"))
    m.Pin = Pin
    m.SPI = FakeSPI
    time.ticks_ms = _ticks_ms
    time.ticks_diff = _ticks_diff
    time.sleep_ms = _sleep_ms
    time.sleep_us = _sleep_us


def reset():
    global clock_ms
    pins.clear()
    log.clear()
    clock_ms = 0


def commands(dc=5):
    """Fold `log` into [(cmd, data)] using the DC pin: DC low = command byte,
    DC high = data appended to the most recent command."""
    out = []
    dc_v = 1
    for ev in log:
        if ev[0] == "pin":
            if ev[1] == dc:
                dc_v = ev[2]
            continue
        _, b = ev
        if dc_v == 0:
            for c in b:
                out.append((c, bytearray()))
        elif out:
            out[-1][1].extend(b)
    return [(c, bytes(d)) for c, d in out]
```

- [ ] **Step 2: Smoke-check it imports and parses**

Run:
```bash
python3 -c "
import sys; sys.path.insert(0,'firmware/tests'); import harness, eink_fakes as f
f.install(); f.reset()
from machine import Pin, SPI
dc=Pin(5,Pin.OUT,value=1); s=SPI(2)
dc.value(0); s.write(b'\x24'); dc.value(1); s.write(b'\x01\x02')
assert f.commands() == [(0x24, b'\x01\x02')], f.commands(); print('ok')"
```
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add firmware/tests/eink_fakes.py
git commit -m "tests: host fakes for framebuf, machine.Pin/SPI and ticks for the e-ink stack"
```

---

### Task 3: SSD1682 driver — power-up, chip-ID gate, init sequence

**Files:**
- Create: `firmware/peripherals/eink_ssd1682.py`
- Test: `firmware/tests/test_eink_ssd1682.py`

**Interfaces:**
- Produces: `class SSD1682` with `WIDTH = 250`, `HEIGHT = 122`, `FULL_EVERY = 10`, `fb` (framebuf, MONO_HLSB, 1 = black), constructor `SSD1682(sck=3, mosi=2, cs=4, dc=5, rst=6, busy=7, vext=45, vext_active=0, rotate=90, xoff=0, miso=21, spi_bus=2, baudrate=4_000_000)`, methods `detect() -> int`, `flush(full=False)`, `clear()`, `power_off()`. This task implements everything except the transpose + flush body, which Task 4 adds; `flush()` in this task is only `self._begin()`.
- Chip ID failure raises `OSError("eink: unsupported panel id 0x%02X" % cid)`.

- [ ] **Step 1: Write the failing tests**

Create `firmware/tests/test_eink_ssd1682.py`:

```python
"""SSD1682 (Heltec E0213A367) e-ink driver — host tests with recorded SPI."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
import eink_fakes as F
F.install()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from peripherals.eink_ssd1682 import SSD1682  # noqa: E402

PINS = dict(sck=3, mosi=2, cs=4, dc=5, rst=6, busy=7, vext=45, vext_active=0, miso=21)
ID_OK = [0, 0, 0, 0, 0, 0, 0, 1]      # 0x01 -> E0213A367
ID_BAD = [0, 0, 0, 0, 0, 0, 1, 0]     # 0x02 -> other panel


def _panel(rotate=90, chip_bits=ID_OK):
    F.reset()
    F.Pin(2).in_bits = list(chip_bits)      # MOSI read back during detect()
    return SSD1682(rotate=rotate, **PINS)


def test_constructor_powers_panel_and_touches_no_spi():
    p = _panel()
    assert ("pin", 45, 0) in F.log, F.log            # VEXT active LOW
    assert F.clock_ms >= 300                         # warm-up waited
    assert not [e for e in F.log if e[0] == "spi"]   # nothing sent yet
    assert p.fb.width == 250 and p.fb.height == 122


def test_detect_reads_id_via_bitbang_and_caches():
    p = _panel()
    assert p.detect() == 0x01
    assert p.detect() == 0x01       # cached: in_bits are exhausted now
    # reset pulse happened before the ID read
    assert ("pin", 6, 0) in F.log and ("pin", 6, 1) in F.log


def test_wrong_panel_id_raises_with_id():
    p = _panel(chip_bits=ID_BAD)
    try:
        p.flush()
    except OSError as e:
        assert "0x02" in str(e), e
    else:
        raise AssertionError("expected OSError")


def test_begin_sends_init_sequence_with_8bit_y_addressing():
    p = _panel()
    p.flush()
    cmds = F.commands(dc=5)
    seq = [(c, d) for c, d in cmds]
    assert seq[0] == (0x12, b""), seq[0]                      # swreset
    assert (0x01, b"\xF9\x00") in seq
    assert (0x3C, b"\x01") in seq
    assert (0x18, b"\x80") in seq
    assert (0x37, b"\x40\x80\x03\x0E") in seq                 # waveform select
    assert (0x11, b"\x03") in seq
    assert (0x44, b"\x00\x0F") in seq
    assert (0x45, b"\x00\xF9") in seq                         # 2 bytes, NOT 4
    assert (0x4E, b"\x00") in seq
    assert (0x4F, b"\x00") in seq                             # 1 byte, NOT 2
    assert (0x10, b"\x01") not in seq and (0x10, b"\xFF") not in seq   # no deep sleep


def _run():
    import traceback
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except Exception as e:
            failed += 1
            print("FAIL  " + name + "  ->  " + repr(e))
            traceback.print_exc()
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 firmware/tests/test_eink_ssd1682.py`
Expected: `ModuleNotFoundError: No module named 'peripherals.eink_ssd1682'`.

- [ ] **Step 3: Write the driver (init half)**

Create `firmware/peripherals/eink_ssd1682.py`:

```python
"""SSD1682 e-paper driver for the Seekink E0213A367 panel (Heltec Wireless
Paper, Vision Master E213 rev. with chip id 0x01). 128(122 visible) x 250
portrait RAM, presented to callers as a 250 x 122 LANDSCAPE framebuffer.

Verified on hardware 2026-10-07 (see docs/superpowers/specs/2026-10-07-
eink-wireless-paper-design.md). Three facts that cost an afternoon:

  * Panel power (VEXT) is GPIO45, ACTIVE LOW, and needs ~300 ms before the
    first command.
  * Y addresses are 8-bit on this controller: 0x45 takes (start, end) = 2
    bytes and 0x4F takes 1 byte. The SSD1680-style 16-bit form silently
    collapses the window to one row and the panel shows noise.
  * 0x37 40 80 03 0E must be sent before the first refresh; it selects the
    OTP waveform. Without it 0x22/0xF7 runs a 510 ms no-op.

Drawing goes to `fb` (framebuf.MONO_HLSB, 1 = BLACK ink) with no panel
traffic. flush() is the only method that talks to the panel: it transposes
to portrait, writes RAM, refreshes and powers the booster off. Full refresh
~1.4 s, fast ~0.5 s; a full one is forced every FULL_EVERY fast ones to
clear ghosting, and always on the first flush after power-up.
"""
import time
import framebuf
import micropython
from machine import Pin, SPI

try:
    ptr8                      # viper pointer type; only a compiler name
except NameError:
    ptr8 = int                # CPython host tests evaluate annotations

_W = 250                      # landscape
_H = 122
_RAM_W = 128                  # portrait RAM width (bits)
_RAM_H = 250
_RAM_BYTES = _RAM_W // 8 * _RAM_H          # 4000
_FB_STRIDE = (_W + 7) // 8                 # 32
_BUSY_TIMEOUT_MS = 10000
_WARMUP_MS = 300


class SSD1682:
    WIDTH = _W
    HEIGHT = _H
    FULL_EVERY = 10

    def __init__(self, sck=3, mosi=2, cs=4, dc=5, rst=6, busy=7,
                 vext=45, vext_active=0, rotate=90, xoff=0, miso=21,
                 spi_bus=2, baudrate=4_000_000):
        if rotate not in (90, 270):
            raise ValueError("rotate must be 90 or 270")
        self._sck_n, self._mosi_n, self._miso_n = sck, mosi, miso
        self._spi_bus, self._baud = spi_bus, baudrate
        self._rot, self._xoff = rotate, xoff
        if vext is not None:
            Pin(vext, Pin.OUT, value=vext_active)
            time.sleep_ms(_WARMUP_MS)
        self._cs = Pin(cs, Pin.OUT, value=1)
        self._dc = Pin(dc, Pin.OUT, value=1)
        self._rst = Pin(rst, Pin.OUT, value=1)
        self._busy = Pin(busy, Pin.IN, Pin.PULL_UP)
        self._spi = None
        self._chip = None
        self._began = False
        self._fast_count = 0
        self._buf = bytearray(_FB_STRIDE * _H)
        self.fb = framebuf.FrameBuffer(self._buf, _W, _H, framebuf.MONO_HLSB)
        self._out = bytearray(_RAM_BYTES)
        self._white = b"\xff" * _RAM_BYTES

    # ----- low level ---------------------------------------------------
    def _hw_reset(self):
        self._rst.value(0)
        time.sleep_ms(20)
        self._rst.value(1)
        time.sleep_ms(20)

    def _wait(self):
        t0 = time.ticks_ms()
        while self._busy.value() == 1:
            if time.ticks_diff(time.ticks_ms(), t0) > _BUSY_TIMEOUT_MS:
                raise OSError("eink busy timeout")
            time.sleep_ms(2)

    def _cmd(self, c, data=None):
        self._dc.value(0)
        self._cs.value(0)
        self._spi.write(bytes([c]))
        self._cs.value(1)
        if data:
            self._data(data)

    def _data(self, buf):
        self._dc.value(1)
        self._cs.value(0)
        self._spi.write(buf)
        self._cs.value(1)

    def _ram_area(self):
        # 8-bit Y addressing on the SSD1682 — see module docstring.
        self._cmd(0x11, b"\x03")                 # x+, y+
        self._cmd(0x44, b"\x00\x0F")             # x 0..15 bytes (128 px)
        self._cmd(0x45, b"\x00\xF9")             # y 0..249   (2 bytes!)
        self._cmd(0x4E, b"\x00")
        self._cmd(0x4F, b"\x00")                 # (1 byte!)

    # ----- panel identification ---------------------------------------
    def detect(self):
        """Chip ID as Heltec's factory firmware reads it: command 0x2F, then
        8 bits clocked in on the (bidirectional) data line. Cached."""
        if self._chip is not None:
            return self._chip
        if self._spi is not None:
            self._spi.deinit()
            self._spi = None
        sck = Pin(self._sck_n, Pin.OUT, value=0)
        mosi = Pin(self._mosi_n, Pin.OUT, value=0)
        self._hw_reset()
        self._dc.value(0)
        self._cs.value(0)
        c = 0x2F
        for i in range(8):
            mosi.value((c >> (7 - i)) & 1)
            sck.value(1)
            time.sleep_us(1)
            sck.value(0)
            time.sleep_us(1)
        time.sleep_ms(10)
        self._dc.value(1)
        mi = Pin(self._mosi_n, Pin.IN, Pin.PULL_UP)
        cid = 0
        for b in range(7, -1, -1):
            sck.value(0)
            time.sleep_us(1)
            sck.value(1)
            time.sleep_us(1)
            if mi.value():
                cid |= 1 << b
        sck.value(0)
        self._cs.value(1)
        self._chip = cid
        return cid

    def _begin(self):
        cid = self.detect()
        if (cid & 0x03) != 0x01:
            raise OSError("eink: unsupported panel id 0x%02X" % cid)
        self._spi = SPI(self._spi_bus, baudrate=self._baud, polarity=0, phase=0,
                        sck=Pin(self._sck_n), mosi=Pin(self._mosi_n),
                        miso=Pin(self._miso_n))
        self._hw_reset()
        self._cmd(0x12)                          # SWRESET
        time.sleep_ms(20)
        self._wait()
        self._cmd(0x01, b"\xF9\x00")             # 250 gates
        self._cmd(0x3C, b"\x01")                 # border white (full mode)
        self._cmd(0x18, b"\x80")                 # internal temperature sensor
        self._cmd(0x37, b"\x40\x80\x03\x0E")     # select OTP waveform
        self._ram_area()
        self._began = True

    # ----- public ------------------------------------------------------
    def flush(self, full=False):
        if not self._began:
            self._begin()

    def clear(self):
        self.fb.fill(0)
        self.flush(full=True)

    def power_off(self):
        """Switch the booster off; the image stays, RAM stays, VEXT stays on."""
        if self._spi is None:
            return
        self._cmd(0x22, b"\x83")
        self._cmd(0x20)
        self._wait()
```

- [ ] **Step 4: Run tests**

Run: `python3 firmware/tests/test_eink_ssd1682.py`
Expected: `4/4 passed`.

- [ ] **Step 5: Commit**

```bash
git add firmware/peripherals/eink_ssd1682.py firmware/tests/test_eink_ssd1682.py
git commit -m "eink: SSD1682 driver skeleton — power-up, chip-ID gate, verified init sequence"
```

---

### Task 4: SSD1682 driver — transpose, flush, refresh policy, timeout

**Files:**
- Modify: `firmware/peripherals/eink_ssd1682.py` (replace `flush()`, add `_transpose`)
- Test: `firmware/tests/test_eink_ssd1682.py` (append tests)

**Interfaces:**
- Consumes: Task 3 class.
- Produces: `flush(full=False)` complete. Command order for a FULL flush after init: `0x3C 01`, area, `0x24`+4000 B, area, `0x26`+4000 B, `0x22 F7`, `0x20`, `0x22 83`, `0x20`. For a FAST flush: `0x3C 81`, area, `0x24`+4000 B, `0x22 FF`, `0x20`, area, `0x26`+4000 B (same bytes, so the next fast refresh diffs against the right "old" image), `0x22 83`, `0x20`. Module function `_transpose(src, dst, rot, xoff)`.

- [ ] **Step 1: Append the failing tests**

Add to `firmware/tests/test_eink_ssd1682.py` above `_run()`:

```python
def _writes(cmd):
    """data payloads of every `cmd` in the recorded log, in order"""
    return [d for c, d in F.commands(dc=5) if c == cmd]


def _refresh_modes():
    return [d for c, d in F.commands(dc=5) if c == 0x22]


def test_first_flush_is_full_even_if_fast_requested():
    p = _panel()
    p.flush(full=False)
    assert _refresh_modes() == [b"\xF7", b"\x83"], _refresh_modes()
    assert (0x3C, b"\x01") in F.commands(dc=5)


def test_full_flush_writes_both_ram_planes_with_4000_bytes():
    p = _panel()
    p.flush(full=True)
    w24, w26 = _writes(0x24), _writes(0x26)
    assert len(w24) == 1 and len(w24[0]) == 4000, len(w24[0])
    assert len(w26) == 1 and w26[0] == w24[0]


def test_blank_framebuffer_is_all_white_in_ram():
    p = _panel()
    p.flush(full=True)
    assert _writes(0x24)[0] == b"\xff" * 4000


def test_transpose_rotate_90_maps_landscape_pixel_to_portrait_bit():
    p = _panel(rotate=90)
    p.fb.pixel(10, 3, 1)            # landscape (x=10, y=3) black
    p.flush(full=True)
    ram = _writes(0x24)[0]
    # rot 90: portrait row py = x = 10, portrait column px = xoff + y = 3
    i = 10 * 16 + (3 >> 3)
    bit = 7 - (3 & 7)
    assert ((ram[i] >> bit) & 1) == 0, "black must be a cleared bit"
    assert sum(1 for b in ram if b != 0xFF) == 1, "exactly one byte touched"


def test_transpose_rotate_270_maps_landscape_pixel_to_portrait_bit():
    p = _panel(rotate=270)
    p.fb.pixel(10, 3, 1)
    p.flush(full=True)
    ram = _writes(0x24)[0]
    py = 249 - 10
    px = 121 - 3
    i = py * 16 + (px >> 3)
    bit = 7 - (px & 7)
    assert ((ram[i] >> bit) & 1) == 0
    assert sum(1 for b in ram if b != 0xFF) == 1


def test_xoff_shifts_portrait_column():
    F.reset()
    F.Pin(2).in_bits = list(ID_OK)
    p = SSD1682(rotate=90, xoff=6, **PINS)
    p.fb.pixel(0, 0, 1)
    p.flush(full=True)
    ram = _writes(0x24)[0]
    assert ((ram[0] >> (7 - 6)) & 1) == 0, hex(ram[0])


def test_fast_flush_sequence_and_old_plane_update():
    p = _panel()
    p.flush(full=True)
    F.log.clear()
    p.fb.pixel(0, 0, 1)
    p.flush()                         # second flush -> fast
    cmds = F.commands(dc=5)
    assert (0x3C, b"\x81") in cmds
    order = [c for c, _ in cmds]
    assert order.index(0x24) < order.index(0x22) < order.index(0x26), order
    assert _refresh_modes() == [b"\xFF", b"\x83"]
    assert _writes(0x26)[0] == _writes(0x24)[0]


def test_full_forced_after_ten_fast_and_counter_resets():
    p = _panel()
    p.flush(full=True)
    modes = []
    for _ in range(12):
        F.log.clear()
        p.flush()
        modes.append(_refresh_modes()[0])
    # 10 fast, then a forced full, then fast again
    assert modes[:10] == [b"\xFF"] * 10, modes
    assert modes[10] == b"\xF7", modes
    assert modes[11] == b"\xFF", modes
    F.log.clear()
    p.flush(full=True)
    for _ in range(3):
        F.log.clear()
        p.flush()
        assert _refresh_modes()[0] == b"\xFF"


def test_power_off_after_every_flush():
    p = _panel()
    p.flush(full=True)
    assert _refresh_modes()[-1] == b"\x83"
    F.log.clear()
    p.flush()
    assert _refresh_modes()[-1] == b"\x83"


def test_busy_stuck_high_raises_instead_of_hanging():
    p = _panel()
    F.Pin(7).stuck = 1
    try:
        p.flush()
    except OSError as e:
        assert "busy timeout" in str(e)
    else:
        raise AssertionError("expected OSError")
    assert F.clock_ms >= 10000


def test_clear_fills_white_and_full_refreshes():
    p = _panel()
    p.fb.fill(1)
    p.clear()
    assert _writes(0x24)[-1] == b"\xff" * 4000
    assert _refresh_modes()[0] == b"\xF7"
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `python3 firmware/tests/test_eink_ssd1682.py`
Expected: the new tests FAIL (`_writes(0x24)` empty, index errors), the 4 earlier ones PASS.

- [ ] **Step 3: Implement transpose + flush**

In `firmware/peripherals/eink_ssd1682.py`, add after the `_FB_STRIDE`/constants block (module level, before the class):

```python
@micropython.viper
def _transpose(src: ptr8, dst: ptr8, rot: int, xoff: int):
    """Landscape 250x122 MONO_HLSB (1 = black) -> portrait 128x250 RAM
    (1 = white). dst must be pre-filled 0xFF; only black pixels clear bits.

    rot 90 : px = xoff + y        py = x
    rot 270: px = xoff + 121 - y  py = 249 - x
    Shifts only, no division: this loop runs 30 500 times per flush."""
    for y in range(122):
        srow = y << 5                        # 32-byte stride
        for x in range(250):
            if (src[srow + (x >> 3)] >> (7 - (x & 7))) & 1:
                if rot == 90:
                    px = xoff + y
                    py = x
                else:
                    px = xoff + 121 - y
                    py = 249 - x
                i = (py << 4) + (px >> 3)
                dst[i] = dst[i] & ~(1 << (7 - (px & 7)))
```

Replace the `flush()` method body:

```python
    def flush(self, full=False):
        """Push `fb` to the glass. Blocks ~1.4 s (full) or ~0.5 s (fast)."""
        if not self._began:
            self._begin()
            full = True                          # unknown glass state
        if self._fast_count >= self.FULL_EVERY:
            full = True
        self._out[:] = self._white
        _transpose(self._buf, self._out, self._rot, self._xoff)
        if full:
            self._cmd(0x3C, b"\x01")             # border white
            self._ram_area()
            self._cmd(0x24, self._out)
            self._ram_area()
            self._cmd(0x26, self._out)
            self._cmd(0x22, b"\xF7")
            self._cmd(0x20)
            self._wait()
            self._fast_count = 0
        else:
            self._cmd(0x3C, b"\x81")             # border floating (fast mode)
            self._ram_area()
            self._cmd(0x24, self._out)
            self._cmd(0x22, b"\xFF")
            self._cmd(0x20)
            self._wait()
            self._ram_area()                     # make "old" plane = current
            self._cmd(0x26, self._out)
            self._fast_count += 1
        self.power_off()
```

Note on viper under CPython: `ptr8 = int` makes the annotations harmless and the body runs on bytearrays with identical semantics (`dst[i] & ~mask` stays in 0..255). On MicroPython, viper resolves `ptr8` by name at compile time and the module-level alias is ignored.

- [ ] **Step 4: Run tests**

Run: `python3 firmware/tests/test_eink_ssd1682.py`
Expected: `15/15 passed`.

- [ ] **Step 5: Byte-compile for the target to catch viper errors early**

Run: `mpy-cross -march=xtensawin -o /tmp/eink_ssd1682.mpy firmware/peripherals/eink_ssd1682.py && echo compiled` (if `mpy-cross` is not installed: `pip3 install --user mpy-cross` or skip and rely on Task 8 on-device import).
Expected: `compiled`. A viper type error prints here instead of at import time on the board.

- [ ] **Step 6: Commit**

```bash
git add firmware/peripherals/eink_ssd1682.py firmware/tests/test_eink_ssd1682.py
git commit -m "eink: viper transpose, full/fast flush with ghosting policy, busy timeout"
```

---

### Task 5: QR copy and icon helpers

**Files:**
- Create: `firmware/peripherals/qr.py` (verbatim copy of `../reticulum-tdeck/qr.py`)
- Create: `firmware/peripherals/eink_icons.py`
- Test: `firmware/tests/test_eink_icons.py`

**Interfaces:**
- Consumes: `qr.encode(text) -> bytearray(49*49)` (1 = dark), `qr.SIZE = 49`, `qr.MAX_BYTES = 192`.
- Produces in `eink_icons`: `ICONS = {"antenna": (16, 16, bytes), "envelope": (16, 12, bytes), "lora": (16, 11, bytes)}`; `icon(fb, name, x, y)`; `rssi_bars(fb, x, y, rssi) -> int` (bars lit, 0–5; `rssi=None` → 0); `BARS_W = 24`, `BARS_H = 12`; `battery(fb, x, y, percent)` (24×12, `percent=None` → empty); `text2x(fb, s, x, y)`; `qr_code(fb, x, y, text, scale=2) -> int` (pixel side length, 98 at scale 2).

- [ ] **Step 1: Copy the QR encoder**

Run:
```bash
cp ../reticulum-tdeck/qr.py firmware/peripherals/qr.py
python3 -c "
import sys; sys.path.insert(0,'firmware/peripherals'); import qr
m = qr.encode('lxma://' + '0'*32 + ':' + '1'*128); assert len(m) == 49*49 and sum(m) > 500; print('qr ok')"
```
Expected: `qr ok`. (The lxma URI is 7 + 32 + 1 + 128 = 168 bytes, under `MAX_BYTES`.)

- [ ] **Step 2: Write the failing tests**

Create `firmware/tests/test_eink_icons.py`:

```python
"""eink_icons: bitmaps, RSSI bars, battery gauge, 2x text, QR blit."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
import eink_fakes as F
F.install()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import framebuf  # noqa: E402  (the shim)
from peripherals import eink_icons as I  # noqa: E402


def _fb(w=250, h=122):
    return framebuf.FrameBuffer(bytearray(((w + 7) // 8) * h), w, h, framebuf.MONO_HLSB)


def _count(fb, x, y, w, h):
    return sum(fb.pixel(xx, yy) for yy in range(y, y + h) for xx in range(x, x + w))


def test_icon_bitmaps_have_declared_size():
    for name, (w, h, bits) in I.ICONS.items():
        assert w % 8 == 0, name
        assert len(bits) == (w // 8) * h, (name, len(bits))
        assert any(bits), name + " is blank"


def test_icon_draws_only_inside_its_box():
    fb = _fb()
    I.icon(fb, "envelope", 100, 50)
    assert _count(fb, 100, 50, 16, 12) > 20
    assert _count(fb, 0, 0, 250, 122) == _count(fb, 100, 50, 16, 12)


def test_rssi_bars_thresholds():
    assert I.rssi_bars(_fb(), 0, 0, None) == 0
    assert I.rssi_bars(_fb(), 0, 0, -130) == 0
    assert I.rssi_bars(_fb(), 0, 0, -120) == 1
    assert I.rssi_bars(_fb(), 0, 0, -105) == 2
    assert I.rssi_bars(_fb(), 0, 0, -95) == 3
    assert I.rssi_bars(_fb(), 0, 0, -85) == 4
    assert I.rssi_bars(_fb(), 0, 0, -60) == 5


def test_rssi_bars_more_signal_more_ink():
    a, b = _fb(), _fb()
    I.rssi_bars(a, 0, 0, -118)
    I.rssi_bars(b, 0, 0, -70)
    assert _count(b, 0, 0, I.BARS_W, I.BARS_H) > _count(a, 0, 0, I.BARS_W, I.BARS_H)


def test_battery_fill_is_proportional_and_none_is_empty():
    e, h, f = _fb(), _fb(), _fb()
    I.battery(e, 0, 0, None)
    I.battery(h, 0, 0, 50)
    I.battery(f, 0, 0, 100)
    ce, ch, cf = (_count(x, 0, 0, 24, 12) for x in (e, h, f))
    assert ce < ch < cf, (ce, ch, cf)
    assert ce > 30, "outline must still be drawn when percent is None"


def test_text2x_doubles_each_glyph_pixel():
    small, big = _fb(), _fb()
    small.text("A", 0, 0, 1)
    I.text2x(big, "A", 0, 0)
    for y in range(8):
        for x in range(8):
            v = small.pixel(x, y)
            for dy in (0, 1):
                for dx in (0, 1):
                    assert big.pixel(2 * x + dx, 2 * y + dy) == v, (x, y)


def test_qr_code_size_and_quiet_zone_untouched():
    fb = _fb()
    side = I.qr_code(fb, 6, 12, "lxma://" + "ab" * 16 + ":" + "cd" * 64, scale=2)
    assert side == 98
    assert _count(fb, 6, 12, 98, 98) > 1000
    assert _count(fb, 0, 0, 6, 122) == 0           # left margin clean
    assert _count(fb, 104, 0, 146, 122) == 0       # nothing right of the code
    # finder pattern: top-left module is dark -> 2x2 block of ink
    assert fb.pixel(6, 12) == 1 and fb.pixel(7, 13) == 1


def _run():
    import traceback
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except Exception as e:
            failed += 1
            print("FAIL  " + name + "  ->  " + repr(e))
            traceback.print_exc()
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
```

- [ ] **Step 3: Run to verify it fails**

Run: `python3 firmware/tests/test_eink_icons.py`
Expected: `ModuleNotFoundError: No module named 'peripherals.eink_icons'`.

- [ ] **Step 4: Write the icons module**

Create `firmware/peripherals/eink_icons.py`:

```python
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
```

- [ ] **Step 5: Run tests**

Run: `python3 firmware/tests/test_eink_icons.py`
Expected: `7/7 passed`.

- [ ] **Step 6: Commit**

```bash
git add firmware/peripherals/qr.py firmware/peripherals/eink_icons.py firmware/tests/test_eink_icons.py
git commit -m "eink: icon bitmaps, RSSI bars, battery gauge, 2x text, QR blit (qr.py from reticulum-tdeck)"
```

---

### Task 6: Status screen renderer

**Files:**
- Create: `firmware/peripherals/eink_status.py`
- Test: `firmware/tests/test_eink_icons.py` (append renderer tests)

**Interfaces:**
- Consumes: `eink_icons` (Task 5).
- Produces: `render(fb, stats)` where `stats` is a dict with keys `name` (str), `hash` (32-hex str), `uri` (lxma URI str), `rx`, `tx` (int), `rssi`, `snr` (number or None), `freq_khz` (int), `sf` (int), `uptime_ms` (int), `vbat` (float or None), `msgs` (int). Also `format_uptime(ms) -> str` (`"up 1h02m"`, `"up 3d04h"`, `"up 45s"`), `battery_percent(v) -> int|None` (3.30 V → 0, 4.20 V → 100, clamped, None passthrough). Layout constants `QR_X = 6`, `QR_Y = 12`, `COL = 112`.

- [ ] **Step 1: Append failing tests**

Add to `firmware/tests/test_eink_icons.py` above `_run()`:

```python
from peripherals import eink_status as S  # noqa: E402

STATS = dict(name="Paper", hash="ab12cd34" * 4, uri="lxma://" + "ab12cd34" * 4 + ":" + "ef" * 64,
             rx=123, tx=45, rssi=-87, snr=9.5, freq_khz=868800, sf=8,
             uptime_ms=3_720_000, vbat=3.95, msgs=0)


def test_format_uptime():
    assert S.format_uptime(45_000) == "up 45s"
    assert S.format_uptime(3_720_000) == "up 1h02m"
    assert S.format_uptime(100 * 3600_000) == "up 4d04h"


def test_battery_percent_clamps_and_passes_none():
    assert S.battery_percent(None) is None
    assert S.battery_percent(3.0) == 0
    assert S.battery_percent(4.5) == 100
    assert 45 <= S.battery_percent(3.75) <= 55


def test_render_draws_qr_frame_and_text_column():
    fb = _fb()
    S.render(fb, STATS)
    assert _count(fb, S.QR_X, S.QR_Y, 98, 98) > 1000          # QR present
    assert fb.pixel(0, 0) == 1 and fb.pixel(249, 121) == 1    # 1 px frame
    assert _count(fb, S.COL, 4, 128, 16) > 0                   # 2x headline
    assert _count(fb, S.COL, 40, 100, 8) > 0                   # RX/TX row


def test_render_long_name_truncates_to_eight_chars():
    fb = _fb()
    s = dict(STATS, name="AVeryLongNodeName")
    S.render(fb, s)
    # 8 chars * 16 px = 128 -> nothing may land right of COL+128 on the headline rows
    assert _count(fb, S.COL + 128, 4, 250 - S.COL - 129, 16) == 0


def test_render_tolerates_unknown_rssi_snr_and_battery():
    fb = _fb()
    s = dict(STATS, rssi=None, snr=None, vbat=None)
    S.render(fb, s)                     # must not raise
    assert _count(fb, 0, 0, 250, 122) > 1000


def test_render_shows_envelope_only_with_messages():
    a, b = _fb(), _fb()
    S.render(a, dict(STATS, msgs=0))
    S.render(b, dict(STATS, msgs=3))
    assert _count(b, 232, 36, 16, 12) > _count(a, 232, 36, 16, 12)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 firmware/tests/test_eink_icons.py`
Expected: `ModuleNotFoundError: No module named 'peripherals.eink_status'`.

- [ ] **Step 3: Write the renderer**

Create `firmware/peripherals/eink_status.py`:

```python
"""Status screen layout for the 250x122 e-ink node (pure drawing, no I/O).

    +------------------------------------------------------+
    | [QR 98px]  NodeName (2x, max 8 chars)                 |
    |            ab12cd34                      ((antenna))  |
    |            RX 123  TX 45                 [envelope]   |
    |            ▂▄▆█  -87dBm 9.5                           |
    |            868.8MHz SF8                               |
    |            up 1h02m                                   |
    |            [battery]  3.95V 72%                       |
    +------------------------------------------------------+

render(fb, stats) takes a plain dict so the demo app stays thin and this file
is testable on the host. Missing values (None) draw as '--'.
"""
from . import eink_icons as icons

QR_X = 6
QR_Y = 12
COL = 112                     # text column start
_ROW = (4, 24, 40, 56, 72, 88, 104)


def format_uptime(ms):
    s = ms // 1000
    if s < 60:
        return "up %ds" % s
    m = s // 60
    h = m // 60
    d = h // 24
    if d:
        return "up %dd%02dh" % (d, h % 24)
    if h:
        return "up %dh%02dm" % (h, m % 60)
    return "up %dm" % m


def battery_percent(v):
    """LiPo 3.30 V -> 0 %, 4.20 V -> 100 %, linear, clamped. None -> None."""
    if v is None:
        return None
    p = int((v - 3.3) * 100 / 0.9 + 0.5)
    return max(0, min(100, p))


def _num(v, fmt):
    return "--" if v is None else fmt % v


def render(fb, s):
    fb.fill(0)
    fb.rect(0, 0, 250, 122, 1)
    icons.qr_code(fb, QR_X, QR_Y, s["uri"], 2)

    icons.text2x(fb, s["name"][:8], COL, _ROW[0])
    fb.text(s["hash"][:8], COL, _ROW[1], 1)
    icons.icon(fb, "antenna", 232, _ROW[1] - 4)

    fb.text("RX %d TX %d" % (s["rx"], s["tx"]), COL, _ROW[2], 1)
    if s.get("msgs"):
        icons.icon(fb, "envelope", 232, _ROW[2] - 4)

    icons.rssi_bars(fb, COL, _ROW[3] - 3, s["rssi"])
    fb.text(_num(s["rssi"], "%ddBm") + " " + _num(s["snr"], "%.1f"), COL + 28, _ROW[3], 1)

    fb.text("%.1fMHz SF%d" % (s["freq_khz"] / 1000, s["sf"]), COL, _ROW[4], 1)
    fb.text(format_uptime(s["uptime_ms"]), COL, _ROW[5], 1)

    pct = battery_percent(s["vbat"])
    icons.battery(fb, COL, _ROW[6] - 1, pct)
    fb.text(_num(s["vbat"], "%.2fV") + " " + _num(pct, "%d%%"), COL + 28, _ROW[6], 1)
```

- [ ] **Step 4: Run tests**

Run: `python3 firmware/tests/test_eink_icons.py`
Expected: `13/13 passed`.

- [ ] **Step 5: Commit**

```bash
git add firmware/peripherals/eink_status.py firmware/tests/test_eink_icons.py
git commit -m "eink: status screen renderer (QR + identity + radio stats + battery)"
```

---

### Task 7: Demo application and config

**Files:**
- Modify: `firmware/config.py` (after `NODE_NAME`, add `EINK`; add a commented board hint next to the LoRa interface entry at ~line 152)
- Create: `firmware/example_eink_node.py`
- Modify: `README.md` (short section)

**Interfaces:**
- Consumes: `SSD1682` (Tasks 3–4), `eink_status.render` (Task 6), `adc_reader.battery_voltage()` (Task 1), `Reticulum.interfaces` list, `LXMRouter` as in `example_node.py`.
- Produces: `config.EINK` dict; the runnable demo `import example_eink_node`.

- [ ] **Step 1: Add `EINK` to config.py**

After the `NODE_NAME = "ESP32s3"` line in `firmware/config.py` insert:

```python
# ---- E-ink panel (example_eink_node.py only) ----
# Heltec Wireless Paper wiring. VEXT (panel power) is GPIO45 and ACTIVE LOW.
# "rotate" picks the landscape orientation (90 or 270); "xoff" shifts the 122
# visible columns inside the 128-wide RAM if the frame looks cut off on one side.
# "miso" is any unconnected GPIO: the panel has no MISO but machine.SPI wants one.
EINK = {"sck": 3, "mosi": 2, "cs": 4, "dc": 5, "rst": 6, "busy": 7,
        "vext": 45, "vext_active": 0, "rotate": 90, "xoff": 0, "miso": 21}
```

And above the LoRa interface's `"board": "xiao_esp32s3_sx1262",` line add:

```python
            # "board": "heltec_wireless_paper",   # Heltec Wireless Paper (e-ink)
```

- [ ] **Step 2: Write the demo**

Create `firmware/example_eink_node.py`:

```python
"""
µReticulum Example: E-Ink Status Node (Heltec Wireless Paper)
=============================================================
Boots the same LXMF node as example_node.py and paints a read-only status
screen on the 2.13" e-ink: a QR code of this node's lxma:// contact URI,
node name and short hash, RX/TX counters, last RSSI/SNR, radio settings,
uptime and battery. No keyboard, no input.

Usage (MicroPython on the Wireless Paper):
  1. Edit config.py: NODE_NAME, set the LoRa interface's
     "board": "heltec_wireless_paper", check EINK pins.
  2. Copy urns/, peripherals/, lora_boards.py, config.py and this file to the
     device; the `lora` driver package to /lib/lora.
  3. Run with: import example_eink_node

The panel redraws when RX/TX/message counters change or every 30 s, never
more often than every 5 s (a fast refresh blocks the event loop ~0.5 s).
"""
from config import WIFI_SSID, WIFI_PASS, NODE_NAME, DEBUG, CONFIG, EINK

import gc
import time
gc.collect()

# ---- Display first: its buffers must exist before crypto fragments the heap ----
from peripherals.eink_ssd1682 import SSD1682
from peripherals import eink_status
panel = SSD1682(**EINK)
gc.collect()

import peripherals.adc_reader as adc_reader
adc_reader.init_battery(CONFIG)
gc.collect()

REDRAW_PERIOD_MS = 30_000
REDRAW_MIN_GAP_MS = 5_000

_msgs = 0            # LXMF messages received since boot


def connect_wifi(ssid, password, timeout=15):
    import network
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.config(pm=0)
    network.WLAN(network.AP_IF).active(False)
    if not wlan.isconnected():
        wlan.connect(ssid, password)
        t0 = time.time()
        while not wlan.isconnected():
            if time.time() - t0 > timeout:
                raise RuntimeError("WiFi connect timeout")
            time.sleep(0.5)
    return wlan.ifconfig()[0]


def needs_wifi(config):
    for iface in config.get("interfaces", []):
        if iface.get("enabled", False) and iface.get("type", "") in (
            "UDPInterface", "TCPClientInterface",
        ):
            return True
    return False


def setup_node(rns, node_name):
    from urns.lxmf import LXMRouter
    router = LXMRouter(identity=rns.identity)
    dest = router.register_delivery_identity(rns.identity, display_name=node_name)

    def on_message(message):
        global _msgs
        _msgs += 1
        if DEBUG >= 1:
            print("<" + message.source_hash.hex()[:8] + "> "
                  + (message.content_as_string() or "(binary)"))
        gc.collect()

    router.register_delivery_callback(on_message)
    return dest, router


def _lora_config(config):
    for iface in config.get("interfaces", []):
        if iface.get("enabled", False) and iface.get("type") == "LoRaInterface":
            return iface
    return {}


def _lora_iface(rns):
    for iface in rns.interfaces:
        if type(iface).__name__ == "LoRaInterface":
            return iface
    return None


def make_stats_fn(rns, dest, lora_cfg):
    uri = "lxma://" + dest.hexhash + ":" + rns.identity.get_public_key().hex()
    t_boot = time.ticks_ms()

    def stats():
        iface = _lora_iface(rns)
        return {
            "name": NODE_NAME,
            "hash": dest.hexhash,
            "uri": uri,
            "rx": getattr(iface, "rx", 0),
            "tx": getattr(iface, "tx", 0),
            "rssi": getattr(iface, "rssi", None),
            "snr": getattr(iface, "snr", None),
            "freq_khz": lora_cfg.get("freq_khz", 868000),
            "sf": lora_cfg.get("sf", 7),
            "uptime_ms": time.ticks_diff(time.ticks_ms(), t_boot),
            "vbat": adc_reader.battery_voltage(),
            "msgs": _msgs,
        }
    return stats


async def display_loop(get_stats):
    import uasyncio as asyncio
    last_key = None
    last_draw = None
    while True:
        s = get_stats()
        now = time.ticks_ms()
        key = (s["rx"], s["tx"], s["msgs"])
        first = last_draw is None
        gap = None if first else time.ticks_diff(now, last_draw)
        due = first or gap >= REDRAW_PERIOD_MS or (key != last_key and gap >= REDRAW_MIN_GAP_MS)
        if due:
            try:
                eink_status.render(panel.fb, s)
                panel.flush(full=first)
            except OSError as e:
                print("eink:", e)
            last_key = key
            last_draw = now
            gc.collect()
        await asyncio.sleep(1)


def main():
    import uasyncio as asyncio

    if needs_wifi(CONFIG):
        connect_wifi(WIFI_SSID, WIFI_PASS)
    gc.collect()

    from urns import Reticulum
    from urns.log import LOG_NONE, LOG_NOTICE, LOG_DEBUG

    log_map = {0: LOG_NONE, 1: LOG_NONE, 2: LOG_DEBUG}
    rns = Reticulum(loglevel=log_map.get(DEBUG, LOG_NOTICE))
    rns.config = CONFIG

    dest, router = setup_node(rns, NODE_NAME)
    gc.collect()
    rns.setup_interfaces()
    gc.collect()

    if DEBUG >= 1:
        print("LXMF address:", dest.hexhash)
        print("Free memory:", gc.mem_free(), "bytes")

    async def announce_loop():
        await asyncio.sleep(0.5)
        while True:
            try:
                router.announce()
                if DEBUG >= 1:
                    print("Announced as:", NODE_NAME)
            except Exception as e:
                if DEBUG >= 2:
                    print("Announce error:", e)
            gc.collect()
            await asyncio.sleep(120)

    _original_run = rns.run

    async def run_all():
        asyncio.create_task(announce_loop())
        asyncio.create_task(display_loop(make_stats_fn(rns, dest, _lora_config(CONFIG))))
        await _original_run()

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        rns.shutdown()
        if DEBUG >= 1:
            print("Shutdown complete")


main()
```

- [ ] **Step 3: Host syntax check of the demo (it cannot run on the host)**

Run: `python3 -m py_compile firmware/example_eink_node.py firmware/config.py && echo syntax-ok`
Expected: `syntax-ok`.

- [ ] **Step 4: README section**

Append to `README.md` after the existing hardware/examples section (find the heading that lists `example_node.py`; add a sibling subsection):

```markdown
### Heltec Wireless Paper (e-ink status node)

`firmware/example_eink_node.py` runs the LXMF node and paints a status screen on
the 2.13" SSD1682 panel: QR code of the node's `lxma://` contact URI, name and
hash, RX/TX counters, last RSSI/SNR, radio settings, uptime and battery.
Set the LoRa interface's `"board": "heltec_wireless_paper"` and check `EINK`
in `config.py`. Panel driver: `peripherals/eink_ssd1682.py` (chip-ID gated,
8-bit Y addressing, full refresh forced every 10 fast ones).
```

- [ ] **Step 5: Commit**

```bash
git add firmware/config.py firmware/example_eink_node.py README.md
git commit -m "example_eink_node: e-ink status screen demo for the Heltec Wireless Paper"
```

---

### Task 8: Hardware bring-up and calibration (manual, on the attached board)

**Files:**
- Modify (if needed): `firmware/config.py` (`EINK["rotate"]`, `EINK["xoff"]`), `firmware/lora_boards.py` (battery `divider`), spec open items.

**Interfaces:**
- Consumes: everything above; the board at `/dev/cu.usbserial-0001` running MicroPython 1.28 (`ESP32_GENERIC_S3`), `../reticulum-tdeck/lib/lora/` driver package, `firmware/lib/ed25519_fast_xtensawin.mpy`, `bz2_fast_xtensawin.mpy`.

- [ ] **Step 1: Deploy**

The board is freshly flashed (nothing to preserve). From the repo root:

```bash
P=/dev/cu.usbserial-0001
mpremote connect $P fs mkdir :/lib 2>/dev/null
mpremote connect $P fs cp -r ../reticulum-tdeck/lib/lora :/lib/
mpremote connect $P fs cp firmware/lib/ed25519_fast_xtensawin.mpy firmware/lib/bz2_fast_xtensawin.mpy :/lib/
mpremote connect $P fs cp -r firmware/urns firmware/peripherals :/
mpremote connect $P fs cp firmware/lora_boards.py firmware/config.py firmware/example_eink_node.py :/
```

Then set on the device (edit a local copy of `config.py` first, do NOT commit credentials): `NODE_NAME = "Paper"`, LoRa interface `"board": "heltec_wireless_paper"`, radio params matching the user's mesh (868 MHz, BW125, SF7, CR5, syncword 0x1424 per memory), `"enable_transport": False`, disable UDP/TCP interfaces. Copy that edited file as `:/config.py`.

- [ ] **Step 2: Driver smoke test on device before running the node**

```bash
mpremote connect $P exec "
from config import EINK
from peripherals.eink_ssd1682 import SSD1682
import time
p = SSD1682(**EINK)
print('chip', hex(p.detect()))
p.fb.rect(0,0,250,122,1); p.fb.rect(2,2,246,118,1)
p.fb.text('ROTATE %d' % EINK['rotate'], 60, 50, 1)
p.fb.fill_rect(0,0,20,20,1)          # top-left marker
t=time.ticks_ms(); p.flush(); print('full flush ms', time.ticks_diff(time.ticks_ms(), t))
p.fb.fill_rect(100,80,40,20,1)
t=time.ticks_ms(); p.flush(); print('fast flush ms', time.ticks_diff(time.ticks_ms(), t))
"
```
Expected: `chip 0x1`, a double frame visible on all four sides with the text upright and the black square in the top-left when the USB connector is on the left. If the text is upside down, set `EINK["rotate"] = 270`. If one long edge of the frame is missing, adjust `xoff` (0…6) until both edges show. Record the two flush times; the transpose share is `full flush ms - 1400`. If it exceeds 150 ms, note it in the spec's open items (keep the implementation).

- [ ] **Step 3: Battery calibration**

With a multimeter on the battery connector (or USB only: expect ~4.1–4.2 V from the charger):
```bash
mpremote connect $P exec "
from config import CONFIG
import peripherals.adc_reader as a
a.init_battery(CONFIG); print(a.battery_voltage())"
```
Set `divider` in the preset so the printed value matches the meter (new = old × meter/printed). Commit the calibrated value with a comment stating the meter reading.

- [ ] **Step 4: Run the demo and verify end to end**

```bash
mpremote connect $P exec "import example_eink_node"
```
Expected within ~60 s: `LXMF address: …`, `Announced as: Paper`, the status screen with QR, name, hash, `RX 0 TX 1`, bars empty, `--dBm --`, freq/SF, uptime, battery. From the user's other LoRa node (Heltec RNode + Sideband/MeshChat) send an announce and a message: RX counter and RSSI/SNR fill in within ~6 s and the envelope icon appears. Scan the QR with Sideband: it must offer to add the contact. Run for ≥5 minutes: no `eink busy timeout`, memory stable (`gc.mem_free()` printed at boot above 150 KB).

- [ ] **Step 5: Record results and commit**

Update the spec's "Open items resolved on hardware" with the chosen `rotate`, `xoff`, divider, and transpose time; commit:

```bash
git add firmware/config.py firmware/lora_boards.py docs/superpowers/specs/2026-10-07-eink-wireless-paper-design.md
git commit -m "eink: hardware-verified rotate/xoff defaults and battery divider for the Wireless Paper"
```

---

### Task 9: Documentation touch-ups

**Files:**
- Modify: `CLAUDE.md` (Architecture → "Key design patterns" or the LoRa board presets bullet)
- Modify: `docs/superpowers/specs/2026-10-07-eink-wireless-paper-design.md` (QR content line)

- [ ] **Step 1: Spec correction**

In the spec, section 4, replace the QR bullet with:

```markdown
- QR content: `lxma://<delivery hash hex>:<identity public key hex>` (168
  bytes), the contact URI Sideband, MeshChatX and Columba scan; same string the
  T-Deck shows. Fits QR version 8-L byte mode (`qr.MAX_BYTES = 192`).
```

- [ ] **Step 2: CLAUDE.md note**

Under "Key design patterns", after the LoRa board presets bullet, add:

```markdown
- **E-ink status node** (`example_eink_node.py`, Heltec Wireless Paper): `peripherals/eink_ssd1682.py` drives the SSD1682 panel via a 250×122 landscape framebuf and transposes on `flush()` only. Gotchas baked in: VEXT GPIO45 is active LOW; Y addresses are **8-bit** (`0x45` 2 bytes, `0x4F` 1 byte — the SSD1680 form shows noise); `0x37 40 80 03 0E` must precede the first refresh; chip-ID (cmd `0x2F`, `(id&3)==1`) gates the driver. Full refresh forced after 10 fast ones. Host tests: `tests/test_eink_ssd1682.py`, `tests/test_eink_icons.py`.
```

- [ ] **Step 3: Run the whole host suite once more and commit**

```bash
for t in firmware/tests/test_*.py; do python3 "$t" >/dev/null || echo "FAILED $t"; done
git add CLAUDE.md docs/superpowers/specs/2026-10-07-eink-wireless-paper-design.md
git commit -m "docs: e-ink node notes in CLAUDE.md; spec QR content = lxma contact URI"
```
Expected: no `FAILED` lines before committing.
