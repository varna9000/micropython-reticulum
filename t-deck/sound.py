# T-Deck I2S Notification Tones
# MAX98357A amplifier on BCK=7, WS=5, DOUT=6
# Soft tones with fade-in/fade-out envelope

import struct
import math
from machine import Pin, I2S

# I2S config
_BCK  = 7
_WS   = 5
_DOUT = 6
_RATE = 8000  # 8kHz sample rate
_BITS = 16


class Sound:

    def __init__(self):
        self.enabled = True
        self._i2s = None
        self._rx_buf = None
        self._tx_buf = None

    def init(self):
        """Initialize I2S and pre-compute tone buffers."""
        self._i2s = I2S(
            0,
            sck=Pin(_BCK),
            ws=Pin(_WS),
            sd=Pin(_DOUT),
            mode=I2S.TX,
            bits=_BITS,
            format=I2S.MONO,
            rate=_RATE,
            ibuf=2048,
        )
        # RX: gentle two-tone chirp (soft ding)
        self._rx_buf = self._gen_chirp(660, 880, 120, 4000)
        # TX: short soft blip
        self._tx_buf = self._gen_tone(440, 80, 3000)

    @staticmethod
    def _gen_tone(freq, duration_ms, amplitude=4000):
        """Generate PCM buffer with fade-in/fade-out envelope."""
        n = _RATE * duration_ms // 1000
        fade = n // 4  # 25% fade in/out
        buf = bytearray(n * 2)
        for i in range(n):
            # Envelope: fade in, sustain, fade out
            if i < fade:
                env = i / fade
            elif i > n - fade:
                env = (n - i) / fade
            else:
                env = 1.0
            val = int(amplitude * env * math.sin(2 * math.pi * freq * i / _RATE))
            struct.pack_into("<h", buf, i * 2, val)
        return buf

    @staticmethod
    def _gen_chirp(f_start, f_end, duration_ms, amplitude=4000):
        """Generate a frequency sweep with fade envelope."""
        n = _RATE * duration_ms // 1000
        fade = n // 4
        buf = bytearray(n * 2)
        for i in range(n):
            t = i / n
            # Envelope
            if i < fade:
                env = i / fade
            elif i > n - fade:
                env = (n - i) / fade
            else:
                env = 1.0
            # Linear frequency sweep
            freq = f_start + (f_end - f_start) * t
            val = int(amplitude * env * math.sin(2 * math.pi * freq * i / _RATE))
            struct.pack_into("<h", buf, i * 2, val)
        return buf

    def play_rx(self):
        """Play incoming message notification."""
        if self.enabled and self._i2s and self._rx_buf:
            self._i2s.write(self._rx_buf)

    def play_tx(self):
        """Play outgoing message notification."""
        if self.enabled and self._i2s and self._tx_buf:
            self._i2s.write(self._tx_buf)

    def deinit(self):
        if self._i2s:
            self._i2s.deinit()
            self._i2s = None
