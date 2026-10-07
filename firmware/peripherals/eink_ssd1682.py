"""SSD1682 e-paper driver for the Seekink E0213A367 panel (Heltec Wireless
Paper, Vision Master E213 rev. with chip id 0x01). 128(122 visible) x 250
portrait RAM, presented to callers as a 250 x 122 LANDSCAPE framebuffer.

Verified on hardware 2026-10-07 (see docs/superpowers/specs/2026-10-07-
eink-wireless-paper-design.md). Three facts that cost an afternoon:

  * Panel power (VEXT) is GPIO45, ACTIVE LOW, and needs ~300 ms before the
    first command. The controller can wedge (ignores RST, swallows RAM
    writes) and only a VEXT power cycle with the logic lines held low
    recovers it, so the constructor always does one.
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
_OFF_MS = 500                 # VEXT off time in the constructor's power cycle


@micropython.viper
def _transpose(src: ptr8, dst: ptr8, rot: int, xoff: int):
    """Landscape 250x122 MONO_HLSB (1 = black) -> portrait 128x250 RAM
    (1 = white). dst is filled with 0xFF here; black pixels then clear bits.

    A rotation is a transpose plus ONE axis flip; a bare transpose mirrors
    the image (verified on glass: mirrored text).
    rot 90 : px = xoff + y        py = 249 - x
    rot 270: px = xoff + 121 - y  py = x
    Shifts only, no division: this loop runs 30 500 times per flush."""
    for i in range(4000):
        dst[i] = 0xFF
    for y in range(122):
        srow = y << 5                        # 32-byte stride
        for x in range(250):
            if (src[srow + (x >> 3)] >> (7 - (x & 7))) & 1:
                if rot == 90:
                    px = xoff + y
                    py = 249 - x
                else:
                    px = xoff + 121 - y
                    py = x
                i = (py << 4) + (px >> 3)
                dst[i] = dst[i] & ~(1 << (7 - (px & 7)))


class SSD1682:
    WIDTH = _W
    HEIGHT = _H
    FULL_EVERY = 10

    def __init__(self, sck=3, mosi=2, cs=4, dc=5, rst=6, busy=7,
                 vext=45, vext_active=0, rotate=90, xoff=0, miso=21,
                 spi_bus=2, baudrate=4_000_000):
        if rotate not in (90, 270):
            raise ValueError("rotate must be 90 or 270")
        if not 0 <= xoff <= _RAM_W - _H:
            # 122 visible columns inside 128 RAM columns; anything else makes
            # the viper transpose index outside the 4000-byte RAM buffer.
            raise ValueError("xoff must be 0..%d" % (_RAM_W - _H))
        self._sck_n, self._mosi_n, self._miso_n = sck, mosi, miso
        self._spi_bus, self._baud = spi_bus, baudrate
        self._rot, self._xoff = rotate, xoff
        if vext is not None:
            # Real power cycle. A wedged controller ignores RST, and it stays
            # parasitically powered through CS/DC/RST/SCK/MOSI unless those are
            # held low while VEXT is off (Heltec's customPowerOff does this).
            for n in (cs, dc, rst, sck, mosi):
                Pin(n, Pin.OUT, value=0)
            vp = Pin(vext, Pin.OUT, value=1 - vext_active)
            time.sleep_ms(_OFF_MS)
            vp.value(vext_active)
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

    def _make_spi(self):
        return SPI(self._spi_bus, baudrate=self._baud, polarity=0, phase=0,
                   sck=Pin(self._sck_n), mosi=Pin(self._mosi_n),
                   miso=Pin(self._miso_n))

    # ----- panel identification ---------------------------------------
    def detect(self):
        """Chip ID as Heltec's factory firmware reads it: command 0x2F, then
        8 bits clocked in on the (bidirectional) data line. Cached."""
        if self._chip is not None:
            return self._chip
        # Claim and release the hardware bus once BEFORE bit-banging. Seen on
        # the ESP32-S3 port: an SPI first initialised on pins that Pin() had
        # already driven as GPIO stays silent (refresh never runs, RAM reads
        # back 0xFF), while a bus created and released beforehand works.
        if self._spi is None:
            self._spi = self._make_spi()
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
        self._spi = self._make_spi()
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
        """Push `fb` to the glass. Blocks ~1.4 s (full) or ~0.5 s (fast)."""
        if not self._began:
            self._begin()
            full = True                          # unknown glass state
        if self._fast_count >= self.FULL_EVERY:
            full = True
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
