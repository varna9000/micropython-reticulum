# µReticulum E32 LoRa Interface
# EByte E32-900T transparent serial LoRa with M0/M1 mode pins, AUX flow control,
# and 6-byte hex register configuration

import time
from .serial import SerialInterface, hdlc_escape, FLAG
from ..log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE


# Air rate codes to bps mapping (E32 SPED register bits 2-0)
_AIR_RATES = {
    0: 300,
    1: 1200,
    2: 2400,
    3: 4800,
    4: 9600,
    5: 19200,
    6: 19200,   # same as 5
    7: 19200,   # same as 5
}

# TX power codes (E32-900T20: 20/17/14/10 dBm)
_TX_POWER = {0: "20dBm", 1: "17dBm", 2: "14dBm", 3: "10dBm"}

# E32 UART cache and chunk sizes
_E32_BUF = 512
_CHUNK_SZ = 200

# UART baud rate codes for SPED register bits 5-3
_BAUD_CODES = {1200: 0, 2400: 1, 4800: 2, 9600: 3, 19200: 4,
               38400: 5, 57600: 6, 115200: 7}


class E32Interface(SerialInterface):

    def __init__(self, config):
        # Extract E32-specific config before calling super
        self._m0_pin_num = config.get("m0_pin", None)
        self._m1_pin_num = config.get("m1_pin", None)
        self._aux_pin_num = config.get("aux_pin", None)
        self._auto_configure = config.get("auto_configure", False)
        self._channel = config.get("channel", 6)
        self._air_rate = config.get("air_rate", 2)
        self._tx_power = config.get("tx_power", 0)

        if config.get("lbt", False):
            log("E32: lbt not supported on E32 modules, ignoring", LOG_ERROR)

        self._m0 = None
        self._m1 = None
        self._aux = None

        self._setup_pins()

        if self._auto_configure:
            self._configure_module(config)

        # Set Mode 0 (transparent) and wait for ready
        self._set_mode(0)
        self._wait_aux_ready(2000)

        # Set bitrate to air rate for Transport throughput calculations
        config["_e32_bitrate"] = _AIR_RATES.get(self._air_rate, 2400)

        # Now call parent — opens UART, sets online=True, starts interface
        super().__init__(config)

        # Override bitrate with air rate (UART baud is irrelevant for throughput)
        self.bitrate = config["_e32_bitrate"]

        log("E32 configured: ch=" + str(self._channel)
            + " air=" + str(_AIR_RATES.get(self._air_rate, "?")) + "bps"
            + " power=" + str(_TX_POWER.get(self._tx_power, "?"))
            , LOG_NOTICE)

    def _setup_pins(self):
        import machine
        from machine import Pin

        if self._m0_pin_num is not None:
            self._m0 = Pin(self._m0_pin_num, Pin.OUT)
            # 12mA drive to overcome E32 internal pull-ups
            addr = 0x4001C000 + (self._m0_pin_num + 1) * 4
            machine.mem32[addr] = (machine.mem32[addr] & ~(0x3 << 4)) | (0x3 << 4)
        if self._m1_pin_num is not None:
            self._m1 = Pin(self._m1_pin_num, Pin.OUT)
            addr = 0x4001C000 + (self._m1_pin_num + 1) * 4
            machine.mem32[addr] = (machine.mem32[addr] & ~(0x3 << 4)) | (0x3 << 4)
        if self._aux_pin_num is not None:
            self._aux = Pin(self._aux_pin_num, Pin.IN, Pin.PULL_UP)

    def _set_mode(self, mode):
        """Set E32 operating mode via M0/M1 pins.
        Mode 0: M0=0 M1=0 (transparent)
        Mode 1: M0=1 M1=0 (wake-up)
        Mode 2: M0=0 M1=1 (power-saving)
        Mode 3: M0=1 M1=1 (sleep / config)
        """
        if self._m0:
            self._m0.value(mode & 1)
        if self._m1:
            self._m1.value((mode >> 1) & 1)
        time.sleep_ms(50)

    def _wait_aux_ready(self, timeout_ms=1000):
        """Wait for AUX pin to go HIGH (idle). Blind delay if no AUX pin."""
        if self._aux is None:
            time.sleep_ms(100)
            return True

        start = time.ticks_ms()
        while not self._aux.value():
            if time.ticks_diff(time.ticks_ms(), start) > timeout_ms:
                log("E32 AUX timeout after " + str(timeout_ms) + "ms", LOG_ERROR)
                return False
            time.sleep_ms(1)
        return True

    def _configure_module(self, config):
        """Enter Mode 3 (sleep) and write 6-byte hex register config to E32."""
        from machine import UART, Pin

        self._set_mode(3)
        time.sleep_ms(200)

        # E32 config mode always uses 9600 8N1
        kwargs = {"baudrate": 9600}
        tx = config.get("tx_pin", None)
        rx = config.get("rx_pin", None)
        if tx is not None and rx is not None:
            kwargs["tx"] = Pin(tx)
            kwargs["rx"] = Pin(rx)
        kwargs["txbuf"] = 256
        kwargs["rxbuf"] = 256

        uart_id = config.get("uart_id", 2)
        cfg_uart = UART(uart_id, **kwargs)

        self._wait_aux_ready(2000)
        time.sleep_ms(100)

        # Read current config
        cfg_uart.write(b'\xC1\xC1\xC1')
        time.sleep_ms(100)
        self._wait_aux_ready(500)
        cur = cfg_uart.read()
        if cur and len(cur) >= 6:
            log("E32 current config: " + " ".join("{:02X}".format(b) for b in cur[:6]), LOG_DEBUG)
        else:
            log("E32 could not read current config", LOG_DEBUG)

        # Build 6-byte register write
        speed = config.get("speed", 9600)
        baud_code = _BAUD_CODES.get(speed, 3)

        # Byte 3: SPED = (parity<<6) | (baud_code<<3) | air_rate
        # parity=0 (8N1)
        sped = (0 << 6) | (baud_code << 3) | (self._air_rate & 0x07)

        # Byte 5: OPTION = (trans_mode<<7) | (io_drive<<6) | (wake_time<<3) | (fec<<2) | tx_power
        # trans_mode=0 (transparent), io_drive=1 (push-pull), wake_time=0 (250ms), fec=1 (enabled)
        option = (0 << 7) | (1 << 6) | (0 << 3) | (1 << 2) | (self._tx_power & 0x03)

        reg = bytes([
            0xC0,                       # Save to flash
            0xFF,                       # ADDH = 0xFF (broadcast)
            0xFF,                       # ADDL = 0xFF (broadcast)
            sped,                       # SPED
            self._channel & 0xFF,       # Channel
            option,                     # OPTION
        ])

        log("E32 writing config: " + " ".join("{:02X}".format(b) for b in reg), LOG_DEBUG)

        cfg_uart.write(reg)
        time.sleep_ms(100)
        self._wait_aux_ready(1000)

        # Read echo/response
        resp = cfg_uart.read()
        if resp:
            log("E32 config response: " + " ".join("{:02X}".format(b) for b in resp), LOG_DEBUG)

        cfg_uart.deinit()
        log("E32 auto-configuration complete", LOG_NOTICE)

    def process_outgoing(self, data):
        """Send HDLC-framed data with AUX flow control and chunking."""
        if not self.online or not self._uart:
            return False

        try:
            data = self.ifac_sign(data)
            frame = bytes([FLAG]) + hdlc_escape(data) + bytes([FLAG])

            if len(frame) > _E32_BUF:
                # Chunk writes with AUX waits between
                offset = 0
                while offset < len(frame):
                    self._wait_aux_ready(2000)
                    chunk = frame[offset:offset + _CHUNK_SZ]
                    self._uart.write(chunk)
                    offset += _CHUNK_SZ
            else:
                self._wait_aux_ready(2000)
                self._uart.write(frame)

            # Wait for E32 to finish transmitting
            self._wait_aux_ready(3000)

            self.txb += len(data)
            self.tx += 1
            self._last_activity = time.time()
            return True
        except Exception as e:
            log("E32 send error: " + str(e), LOG_ERROR)
            return False

    def close(self):
        """Shutdown: set Mode 3 (sleep) and close UART."""
        if self._m0 or self._m1:
            self._set_mode(3)
        super().close()
        log("E32 Interface " + self.name + " closed (sleep mode)", LOG_VERBOSE)

    def __str__(self):
        return "E32Interface[" + self.name + "]"
