# µReticulum E220 LoRa Interface
# EByte E220-900T transparent serial LoRa with AUX flow control and AT config

import time
from .serial import SerialInterface, hdlc_escape, FLAG
from ..log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE


# Air rate codes to bps mapping
_AIR_RATES = {
    0: 2400,   # 2.4k (same as 2, kept for compat)
    1: 2400,
    2: 2400,
    3: 4800,
    4: 9600,
    5: 19200,
    6: 38400,
    7: 62500,
}

# TX power codes (for logging)
_TX_POWER = {0: "max", 1: "-4dB", 2: "-8dB", 3: "-12dB"}

# E220 buffer limit
_E220_BUF = 400
_CHUNK_SZ = 200


class E220Interface(SerialInterface):

    def __init__(self, config):
        # Extract E220-specific config before calling super
        self._m0_pin_num = config.get("m0_pin", None)
        self._m1_pin_num = config.get("m1_pin", None)
        self._aux_pin_num = config.get("aux_pin", None)
        self._auto_configure = config.get("auto_configure", False)
        self._channel = config.get("channel", 18)
        self._air_rate = config.get("air_rate", 2)
        self._tx_power = config.get("tx_power", 0)
        self._lbt = config.get("lbt", True)

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
        config["_e220_bitrate"] = _AIR_RATES.get(self._air_rate, 2400)

        # Now call parent — opens UART, sets online=True, starts interface
        super().__init__(config)

        # Override bitrate with air rate (UART baud is irrelevant for throughput)
        self.bitrate = config["_e220_bitrate"]

        log("E220 configured: ch=" + str(self._channel)
            + " air=" + str(_AIR_RATES.get(self._air_rate, "?")) + "bps"
            + " power=" + str(_TX_POWER.get(self._tx_power, "?"))
            + " lbt=" + str(self._lbt), LOG_NOTICE)

    def _setup_pins(self):
        from machine import Pin

        if self._m0_pin_num is not None:
            self._m0 = Pin(self._m0_pin_num, Pin.OUT)
        if self._m1_pin_num is not None:
            self._m1 = Pin(self._m1_pin_num, Pin.OUT)
        if self._aux_pin_num is not None:
            self._aux = Pin(self._aux_pin_num, Pin.IN, Pin.PULL_UP)

    def _set_mode(self, mode):
        """Set E220 operating mode via M0/M1 pins.
        Mode 0: M0=0 M1=0 (transparent)
        Mode 3: M0=1 M1=1 (AT config)
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
                log("E220 AUX timeout after " + str(timeout_ms) + "ms", LOG_ERROR)
                return False
            time.sleep_ms(1)
        return True

    def _configure_module(self, config):
        """Enter Mode 3 and send AT commands to configure the E220."""
        from machine import UART, Pin

        self._set_mode(3)
        time.sleep_ms(200)

        # Mode 3 forces 9600 8N1
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

        speed = config.get("speed", 9600)
        # UART baud rate code: 9600=3, 19200=4, 38400=5, 57600=6, 115200=7
        baud_codes = {1200: 0, 2400: 1, 4800: 2, 9600: 3, 19200: 4,
                      38400: 5, 57600: 6, 115200: 7}
        baud_code = baud_codes.get(speed, 3)

        cmds = [
            "AT+UART=" + str(baud_code) + ",0,0",       # baud, parity=8N1, stopbits=1
            "AT+RATE=" + str(self._air_rate),             # air data rate
            "AT+CHANNEL=" + str(self._channel),           # frequency channel
            "AT+ADDR=65535",                               # broadcast address
            "AT+TRANS=0",                                  # transparent mode
            "AT+PACKET=0",                                 # packet length = 200 (default)
            "AT+POWER=" + str(self._tx_power),             # TX power
            "AT+LBT=" + ("1" if self._lbt else "0"),       # listen-before-talk
        ]

        for cmd in cmds:
            cfg_uart.write(cmd + "\r\n")
            time.sleep_ms(100)
            self._wait_aux_ready(500)
            # Read any response
            resp = cfg_uart.read()
            if resp:
                log("E220 AT: " + cmd + " -> " + str(resp), LOG_DEBUG)
            else:
                log("E220 AT: " + cmd + " (no response)", LOG_DEBUG)

        cfg_uart.deinit()
        log("E220 auto-configuration complete", LOG_NOTICE)

    def process_outgoing(self, data):
        """Send HDLC-framed data with AUX flow control and chunking."""
        if not self.online or not self._uart:
            return False

        try:
            frame = bytes([FLAG]) + hdlc_escape(data) + bytes([FLAG])

            if len(frame) > _E220_BUF:
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

            self.txb += len(data)
            self.tx += 1
            self._last_activity = time.time()
            return True
        except Exception as e:
            log("E220 send error: " + str(e), LOG_ERROR)
            return False

    def close(self):
        """Shutdown: set Mode 3 (sleep) and close UART."""
        # Set sleep mode before closing
        if self._m0 or self._m1:
            self._set_mode(3)
        super().close()
        log("E220 Interface " + self.name + " closed (sleep mode)", LOG_VERBOSE)

    def __str__(self):
        return "E220Interface[" + self.name + "]"
