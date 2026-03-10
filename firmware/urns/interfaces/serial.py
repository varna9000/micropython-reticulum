# µReticulum Serial Interface
# HDLC-framed serial communication, wire-compatible with RNS SerialInterface

import time
from . import Interface
from ..log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE, LOG_EXTREME


# Simplified HDLC framing (same as reference RNS)
FLAG     = 0x7E
ESC      = 0x7D
ESC_MASK = 0x20


def hdlc_escape(data):
    """Escape FLAG and ESC bytes in data"""
    out = bytearray()
    for b in data:
        if b == FLAG:
            out.append(ESC)
            out.append(FLAG ^ ESC_MASK)
        elif b == ESC:
            out.append(ESC)
            out.append(ESC ^ ESC_MASK)
        else:
            out.append(b)
    return bytes(out)


class SerialInterface(Interface):
    HW_MTU = 564

    def __init__(self, config):
        name = config.get("name", "Serial")
        super().__init__(name)

        self.speed = config.get("speed", 115200)
        self.databits = config.get("databits", 8)
        self.parity = config.get("parity", None)
        self.stopbits = config.get("stopbits", 1)
        self.bitrate = self.speed

        # MicroPython UART config
        self.uart_id = config.get("uart_id", 1)
        self.tx_pin = config.get("tx_pin", None)
        self.rx_pin = config.get("rx_pin", None)

        # Read timeout for stale buffer (ms)
        self.timeout = config.get("timeout", 100)

        # State
        self._uart = None
        self._in_frame = False
        self._escape = False
        self._buffer = bytearray()
        self._last_read_ms = 0

        self._open_port()

    # Map config parity strings to MicroPython UART integers
    _PARITY_MAP = {"E": 0, "e": 0, "O": 1, "o": 1}

    def _open_port(self):
        """Open serial port via MicroPython machine.UART"""
        from machine import UART, Pin

        kwargs = {"baudrate": self.speed}

        if self.tx_pin is not None and self.rx_pin is not None:
            kwargs["tx"] = Pin(self.tx_pin)
            kwargs["rx"] = Pin(self.rx_pin)

        kwargs["txbuf"] = 1024
        kwargs["rxbuf"] = 1024

        if self.databits != 8:
            kwargs["bits"] = self.databits

        if self.parity and self.parity in self._PARITY_MAP:
            kwargs["parity"] = self._PARITY_MAP[self.parity]

        if self.stopbits and self.stopbits != 1:
            kwargs["stop"] = self.stopbits

        self._uart = UART(self.uart_id, **kwargs)
        self.online = True
        log("Serial port UART" + str(self.uart_id) + " opened at " + str(self.speed) + " baud", LOG_NOTICE)

    def process_outgoing(self, data):
        """Send HDLC-framed data"""
        if not self.online or not self._uart:
            return False

        try:
            frame = bytes([FLAG]) + hdlc_escape(data) + bytes([FLAG])
            self._uart.write(frame)
            self.txb += len(data)
            self.tx += 1
            self._last_activity = time.time()
            return True
        except Exception as e:
            log("Serial send error: " + str(e), LOG_ERROR)
            return False

    def _process_byte(self, byte):
        """Process one incoming byte through HDLC state machine"""
        if self._in_frame and byte == FLAG:
            # End of frame
            self._in_frame = False
            if len(self._buffer) > 0:
                self.process_incoming(bytes(self._buffer))
                self._buffer = bytearray()

        elif byte == FLAG:
            # Start of frame
            self._in_frame = True
            self._buffer = bytearray()
            self._escape = False

        elif self._in_frame and len(self._buffer) < self.HW_MTU:
            if byte == ESC:
                self._escape = True
            else:
                if self._escape:
                    if byte == FLAG ^ ESC_MASK:
                        byte = FLAG
                    elif byte == ESC ^ ESC_MASK:
                        byte = ESC
                    self._escape = False
                self._buffer.append(byte)

    def _read_available(self):
        """Read and process all available bytes"""
        while self._uart.any():
            chunk = self._uart.read(self._uart.any())
            if chunk:
                self._last_read_ms = time.ticks_ms()
                for b in chunk:
                    self._process_byte(b)

    def _check_timeout(self):
        """Clear stale buffer on timeout"""
        if len(self._buffer) > 0:
            elapsed = time.ticks_diff(time.ticks_ms(), self._last_read_ms)

            if elapsed > self.timeout:
                self._buffer = bytearray()
                self._in_frame = False
                self._escape = False

    MAX_ERROR_RETRIES = 5
    MAX_REOPEN_RETRIES = 3

    async def poll_loop(self):
        """Async poll loop for incoming serial data"""
        import uasyncio as asyncio

        log("Serial poll loop started for " + self.name, LOG_VERBOSE)

        _err_count = 0
        _reopen_count = 0

        while self.online:
            try:
                had_data = self._uart.any() if self._uart else False
                self._read_available()
                self._check_timeout()
                if had_data:
                    _err_count = 0
            except Exception as e:
                _err_count += 1
                log("Serial poll error (" + str(_err_count) + "/"
                    + str(self.MAX_ERROR_RETRIES) + "): " + str(e), LOG_ERROR)

                if _err_count >= self.MAX_ERROR_RETRIES:
                    _reopen_count += 1
                    if _reopen_count > self.MAX_REOPEN_RETRIES:
                        log("Serial UART reopen retries exhausted, giving up", LOG_ERROR)
                        self.online = False
                        break

                    log("Serial UART reopening (" + str(_reopen_count) + "/"
                        + str(self.MAX_REOPEN_RETRIES) + ")", LOG_NOTICE)
                    try:
                        self._open_port()
                        _err_count = 0
                    except Exception as e2:
                        log("Serial UART reopen failed: " + str(e2), LOG_ERROR)

            await asyncio.sleep(0.005)  # 5ms poll interval for serial

        log("Serial poll loop EXITED for " + self.name, LOG_ERROR)

    def close(self):
        super().close()
        if self._uart:
            try:
                self._uart.deinit()
            except:
                pass
            self._uart = None
            log("Serial Interface " + self.name + " closed", LOG_VERBOSE)

    def __str__(self):
        return "SerialInterface[" + self.name + "]"
