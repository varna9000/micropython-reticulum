# µReticulum SX1262 SPI LoRa Interface
# Direct SPI control via micropython-lib lora-sx126x driver.
# Install: mpremote mip install lora-sx126x
#
# Fragmentation: SX1262 max packet = 255 bytes, Reticulum MTU = 500.
# 1-byte header per LoRa frame: bits 7-6 = type, bits 5-0 = seq (0-63).

import gc
import time
from . import Interface
from ..log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE

# Fragment types (upper 2 bits)
_SINGLE = const(0x00)
_FIRST  = const(0x40)
_MIDDLE = const(0x80)
_LAST   = const(0xC0)

_TYPE_MASK = const(0xC0)
_SEQ_MASK  = const(0x3F)

# Max payload per LoRa frame (255 - 1 byte header)
_FRAG_PAYLOAD = const(254)

# Reassembly timeout (seconds)
_REASM_TIMEOUT = const(15)


class LoRaInterface(Interface):

    def __init__(self, config):
        name = config.get("name", "LoRa SX1262")
        super().__init__(name)

        # SPI bus and pin numbers
        self._spi_bus = config.get("spi_bus", 1)
        self._sck_pin = config.get("sck_pin", 7)
        self._mosi_pin = config.get("mosi_pin", 9)
        self._miso_pin = config.get("miso_pin", 8)

        # SX1262 control pins
        self._cs_pin = config.get("cs_pin", 41)
        self._busy_pin = config.get("busy_pin", 40)
        self._dio1_pin = config.get("dio1_pin", 39)
        self._reset_pin = config.get("reset_pin", 42)

        # DIO2/DIO3 options
        self._dio2_rf_sw = config.get("dio2_rf_sw", True)
        self._dio3_tcxo_mv = config.get("dio3_tcxo_millivolts", 1800)

        # Radio parameters
        self._freq_khz = config.get("freq_khz", 868000)
        self._sf = config.get("sf", 7)
        self._bw = config.get("bw", "125")
        self._coding_rate = config.get("coding_rate", 5)
        self._tx_power = config.get("tx_power", 14)
        self._preamble_len = config.get("preamble_len", 8)
        self._crc_en = config.get("crc_en", True)
        self._syncword = config.get("syncword", 0x1424)

        self._modem = None

        # Fragmentation: disabled by default for RNode interop.
        # RNode sends raw Reticulum packets with no fragment header.
        # Enable only for SX1262-to-SX1262 links needing MTU > 255.
        self._fragment_en = config.get("fragment", False)

        # Fragment TX state
        self._seq_counter = 0

        # Reassembly RX state
        self._reasm_buf = None
        self._reasm_seq = None
        self._reasm_time = 0

        try:
            self._init_modem()
            self.online = True
            log("LoRa " + self.name + " on " + str(self._freq_khz) + "kHz"
                + " SF" + str(self._sf) + " BW" + str(self._bw)
                + " TX" + str(self._tx_power) + "dBm", LOG_NOTICE)
        except Exception as e:
            log("LoRa modem init failed: " + str(e), LOG_ERROR)
            self.online = False

    def _init_modem(self):
        from machine import SPI, Pin
        from lora import SX1262

        spi = SPI(
            self._spi_bus,
            baudrate=2_000_000,
            sck=Pin(self._sck_pin),
            mosi=Pin(self._mosi_pin),
            miso=Pin(self._miso_pin),
        )

        kwargs = {
            "spi": spi,
            "cs": Pin(self._cs_pin, Pin.OUT, value=1),
            "busy": Pin(self._busy_pin, Pin.IN),
            "dio1": Pin(self._dio1_pin, Pin.IN),
            "reset": Pin(self._reset_pin, Pin.OUT, value=1),
            "dio2_rf_sw": self._dio2_rf_sw,
            "lora_cfg": {
                "freq_khz": self._freq_khz,
                "sf": self._sf,
                "bw": self._bw,
                "coding_rate": self._coding_rate,
                "output_power": self._tx_power,
                "preamble_len": self._preamble_len,
                "crc_en": self._crc_en,
                "syncword": self._syncword,
            },
        }
        kwargs["dio3_tcxo_millivolts"] = self._dio3_tcxo_mv

        self._modem = SX1262(**kwargs)
        self._modem.rx_crc_error = True  # Surface CRC-failed packets for diagnostics
        self._modem.start_recv(continuous=True)

    def _next_seq(self):
        seq = self._seq_counter & _SEQ_MASK
        self._seq_counter = (self._seq_counter + 1) & _SEQ_MASK
        return seq

    def _fragment(self, data):
        """Split data into LoRa-sized frames with 1-byte fragment header."""
        if len(data) <= _FRAG_PAYLOAD:
            seq = self._next_seq()
            return [bytes([_SINGLE | seq]) + data]

        seq = self._next_seq()
        frames = []
        offset = 0

        # First fragment
        frames.append(bytes([_FIRST | seq]) + data[offset:offset + _FRAG_PAYLOAD])
        offset += _FRAG_PAYLOAD

        # Middle fragments (not needed at MTU=500, but correct for any size)
        while offset + _FRAG_PAYLOAD < len(data):
            frames.append(bytes([_MIDDLE | seq]) + data[offset:offset + _FRAG_PAYLOAD])
            offset += _FRAG_PAYLOAD

        # Last fragment
        frames.append(bytes([_LAST | seq]) + data[offset:])
        return frames

    def _reassemble(self, raw):
        """Process a received LoRa frame. Returns complete packet or None."""
        if len(raw) < 2:
            return None

        hdr = raw[0]
        ftype = hdr & _TYPE_MASK
        seq = hdr & _SEQ_MASK
        payload = raw[1:]

        if ftype == _SINGLE:
            return payload

        now = time.time()

        if ftype == _FIRST:
            self._reasm_buf = bytearray(payload)
            self._reasm_seq = seq
            self._reasm_time = now
            return None

        # MIDDLE or LAST — must match current reassembly
        if self._reasm_buf is None or self._reasm_seq != seq:
            self._reasm_buf = None
            return None

        if now - self._reasm_time > _REASM_TIMEOUT:
            log("LoRa reassembly timeout", LOG_DEBUG)
            self._reasm_buf = None
            return None

        self._reasm_buf.extend(payload)

        if ftype == _LAST:
            result = bytes(self._reasm_buf)
            self._reasm_buf = None
            self._reasm_seq = None
            return result

        # MIDDLE — keep accumulating
        return None

    def process_outgoing(self, data):
        if not self.online or not self._modem:
            return False

        try:
            # Prepend dummy byte to compensate for SX1262 FIFO
            # write-offset artifact (symmetric with RX strip).
            data = b'\x00' + data

            if self._fragment_en:
                frames = self._fragment(data)
                for frame in frames:
                    self._modem.send(frame)
                log("LoRa sent " + str(len(data) - 1) + "B in "
                    + str(len(frames)) + " frame(s)", LOG_DEBUG)
            else:
                if len(data) > 256:
                    log("LoRa drop: " + str(len(data) - 1) + "B exceeds 255 (fragment=False)", LOG_ERROR)
                    return False
                self._modem.send(data)
                log("LoRa sent " + str(len(data) - 1) + "B raw", LOG_DEBUG)
                log("LoRa TX raw[0:20]=" + data[1:21].hex(), LOG_DEBUG)

            self._modem.start_recv(continuous=True)
            self.txb += len(data) - 1
            self.tx += 1
            self._last_activity = time.time()
            return True
        except Exception as e:
            log("LoRa send error: " + str(e), LOG_ERROR)
            try:
                self._modem.start_recv(continuous=True)
            except:
                pass
            return False

    async def poll_loop(self):
        import uasyncio as asyncio

        log("LoRa poll loop started for " + self.name
            + " fragment=" + str(self._fragment_en), LOG_NOTICE)

        _last_gc = time.time()
        _last_diag = time.time()
        _rx_true_count = 0
        _rx_pkt_count = 0

        while self.online:
            try:
                now = time.time()

                # Periodic GC
                if now - _last_gc >= 10:
                    gc.collect()
                    _last_gc = now

                # Periodic diagnostics
                if now - _last_diag >= 10:
                    _crc_errs = getattr(self._modem, "crc_errors", 0)
                    log("LoRa diag: poll_recv True=" + str(_rx_true_count)
                        + " pkts=" + str(_rx_pkt_count)
                        + " crc_err=" + str(_crc_errs), LOG_DEBUG)
                    _rx_true_count = 0
                    _rx_pkt_count = 0
                    _last_diag = now

                # Stale reassembly cleanup (only when fragmentation enabled)
                if (self._fragment_en
                        and self._reasm_buf is not None
                        and now - self._reasm_time > _REASM_TIMEOUT):
                    log("LoRa discarding stale fragment", LOG_DEBUG)
                    self._reasm_buf = None
                    self._reasm_seq = None

                rx = self._modem.poll_recv()

                if rx is False:
                    log("LoRa modem stopped receiving, restarting", LOG_ERROR)
                    self._modem.start_recv(continuous=True)

                elif rx is True:
                    _rx_true_count += 1

                elif rx and rx is not True:
                    if hasattr(rx, "rssi"):
                        self.rssi = rx.rssi
                    if hasattr(rx, "snr"):
                        self.snr = rx.snr

                    raw = bytes(rx)
                    log("LoRa RX raw " + str(len(raw)) + "B"
                        + " RSSI=" + str(getattr(rx, "rssi", "?"))
                        + " SNR=" + str(getattr(rx, "snr", "?")), LOG_DEBUG)
                    log("LoRa RX hex[0:20]=" + raw[:20].hex(), LOG_DEBUG)

                    if hasattr(rx, "valid_crc") and not rx.valid_crc:
                        log("LoRa CRC fail, discarding", LOG_DEBUG)
                        await asyncio.sleep(0.05)
                        continue

                    # The lora-sx126x driver prepends one spurious byte
                    # when reading from the SX1262 FIFO (buffer-offset
                    # artifact).  Byte 0 varies across receptions of the
                    # same packet while bytes 1+ are the real payload.
                    if len(raw) > 1:
                        raw = raw[1:]
                    else:
                        await asyncio.sleep(0.05)
                        continue

                    if self._fragment_en:
                        pkt = self._reassemble(raw)
                    else:
                        pkt = raw

                    if pkt is not None:
                        _rx_pkt_count += 1
                        log("LoRa recv " + str(len(pkt)) + "B"
                            + " RSSI=" + str(self.rssi)
                            + " SNR=" + str(self.snr), LOG_DEBUG)
                        self.process_incoming(pkt)
                        gc.collect()

            except Exception as e:
                log("LoRa poll error: " + str(e), LOG_ERROR)

            await asyncio.sleep(0.05)

        log("LoRa poll loop EXITED for " + self.name, LOG_ERROR)

    def close(self):
        super().close()
        if self._modem:
            try:
                self._modem.sleep()
            except:
                pass
        log("LoRa " + self.name + " closed", LOG_VERBOSE)

    def __str__(self):
        return "LoRaInterface[" + self.name + "]"
