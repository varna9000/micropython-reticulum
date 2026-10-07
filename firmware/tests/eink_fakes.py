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
