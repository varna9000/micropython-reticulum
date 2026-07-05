# µReticulum SX1262 SPI LoRa Interface
# Direct SPI control via micropython-lib lora-sx126x driver.
# Install: mpremote mip install lora-sx126x lora-sync
#
# RNode-compatible framing: 1-byte header per LoRa frame.
# Upper nibble = random sequence, bit 0 = FLAG_SPLIT.
# Packets > 254 bytes are split across exactly 2 frames (max 508B).
# Compatible with RNode firmware and reference Reticulum.
#
# CSMA / listen-before-talk: every frame TX is gated on a live-RSSI channel
# probe with bounded random backoff (see _lbt_wait) — same etiquette as RNode
# firmware's CSMA. The radio is half-duplex and would otherwise transmit
# blind into ongoing transmissions (e.g. a repeater still re-transmitting
# the frame we are replying to).

import os
import gc
import time
from micropython import const
from . import Interface
from ..log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE

# RNode header constants (matches RNode_Firmware Framing.h)
_FLAG_SPLIT = const(0x01)
_SEQ_MASK   = const(0xF0)

# Max payload per LoRa frame (255 - 1 byte RNode header)
_FRAME_PAYLOAD = const(254)

# Reassembly timeout (seconds)
_REASM_TIMEOUT = const(15)

# Window (seconds) in which verbatim copies of a just-completed split packet
# (e.g. from a transparent repeater) are recognised and dropped.
_REASM_DUP_WINDOW = const(5)


class LoRaInterface(Interface):

    def __init__(self, config):
        name = config.get("name", "LoRa SX1262")
        super().__init__(name)

        # Max on-air RNS packet: RNode protocol splits >254B into 2 frames
        # (max 508B total). Used for link-MTU clamping at transit.
        self.HW_MTU = 508

        # External SPI + bus arbitration (for shared SPI, e.g. T-Deck)
        self._external_spi = config.get("spi", None)
        self._spi_acquire = config.get("spi_acquire", None)
        self._spi_release = config.get("spi_release", None)

        # SPI bus and pin numbers
        self._spi_bus = config.get("spi_bus", 1)
        self._spi_baudrate = config.get("spi_baudrate", 2_000_000)
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
        self._use_dcdc = config.get("use_dcdc", False)

        # Radio parameters
        self._freq_khz = config.get("freq_khz", 868000)
        self._sf = config.get("sf", 7)
        self._bw = config.get("bw", "125")
        self._coding_rate = config.get("coding_rate", 5)
        self._tx_power = config.get("tx_power", 14)
        self._preamble_len = config.get("preamble_len", 8)
        self._crc_en = config.get("crc_en", True)
        self._syncword = config.get("syncword", 0x1424)

        # Listen-before-talk (CSMA). Before each frame TX the modem's live
        # RSSI is probed; while it reads at or above lbt_rssi dBm the send
        # is deferred in short random slots, up to lbt_max_ms total (then
        # transmit regardless). Protects request/reply turnarounds (link
        # proofs, LXMF acks) from colliding with any station we can hear.
        # Set "lbt_rssi": None to disable.
        self._lbt_rssi = config.get("lbt_rssi", -100)
        self._lbt_max_ms = config.get("lbt_max_ms", 2000)
        self._lbt_waits = 0    # sends that deferred at least one slot
        self._lbt_forced = 0   # sends forced after waiting the full cap

        self._modem = None

        # Split-packet reassembly state
        self._reasm_buf = None
        self._reasm_seq = None
        self._reasm_time = 0
        # Fingerprint of the last completed split packet, to drop verbatim
        # repeater copies of its halves: (seq, len1, head1, len2, head2, time)
        self._reasm_done = None

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

        if self._external_spi:
            spi = self._external_spi
        else:
            spi = SPI(
                self._spi_bus,
                baudrate=self._spi_baudrate,
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
        if self._dio3_tcxo_mv is not None:
            kwargs["dio3_tcxo_millivolts"] = self._dio3_tcxo_mv

        self._modem = SX1262(**kwargs)

        # Set DC-DC regulator mode (opcode 0x96, value 0x01).
        # The lora-sx126x driver defaults to LDO which is insufficient
        # for TX on many boards (e.g. T-Deck SX1262).
        if self._use_dcdc:
            import time
            self._modem._cmd("BB", 0x96, 0x01)
            time.sleep_ms(5)
            self._modem.calibrate()
            self._modem.calibrate_image()
            time.sleep_ms(10)
            # Reconfigure after regulator/calibration change
            self._modem.configure(kwargs["lora_cfg"])

        self._modem.rx_crc_error = True  # Surface CRC-failed packets for diagnostics
        self._modem.start_recv(continuous=True)

    def _acquire(self):
        if self._spi_acquire:
            self._spi_acquire()

    def _release(self):
        if self._spi_release:
            self._spi_release()

    def _channel_busy(self):
        """One live-RSSI probe (SX126x GetRssiInst, opcode 0x15) while the
        modem sits in continuous RX. Raw value maps to -raw/2 dBm; busy when
        at or above the lbt_rssi threshold. A failed probe reads as clear so
        LBT can never block transmission."""
        try:
            res = self._modem._cmd("B", 0x15, n_read=2)   # [status, rssi_raw]
            return res[1] <= (-2 * self._lbt_rssi)
        except Exception:
            return False

    def _lbt_wait(self):
        """Listen-before-talk: while the channel shows RF energy, sleep a
        random 15-64ms slot and re-probe, bounded by lbt_max_ms (then send
        anyway so a jammed channel can't stall the node). Random slots keep
        two waiting nodes from firing simultaneously when the channel clears.
        Blocks the event loop like send() itself does — bounded and brief."""
        if self._lbt_rssi is None:
            return
        deadline = time.ticks_add(time.ticks_ms(), self._lbt_max_ms)
        waited = False
        while True:
            self._acquire()
            busy = self._channel_busy()
            self._release()
            if not busy:
                break
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                self._lbt_forced += 1
                log("LoRa LBT cap hit, transmitting anyway", LOG_DEBUG)
                break
            waited = True
            time.sleep_ms(15 + (os.urandom(1)[0] % 50))
        if waited:
            self._lbt_waits += 1

    def _send_frame(self, frame):
        """Single physical frame TX, LBT-gated, modem left in continuous RX
        (the RSSI probe requires RX mode, so this invariant feeds _lbt_wait)."""
        self._lbt_wait()
        self._acquire()
        try:
            self._modem.send(frame)
            self._modem.start_recv(continuous=True)
        finally:
            self._release()

    def process_outgoing(self, data):
        if not self.online or not self._modem:
            return False

        if len(data) > 2 * _FRAME_PAYLOAD:
            log("LoRa drop: " + str(len(data)) + "B exceeds " + str(2 * _FRAME_PAYLOAD), LOG_DEBUG)
            return False

        data = self.ifac_sign(data)

        # RNode-compatible header: random seq in upper nibble.
        # The SX1262 driver sends all bytes faithfully — no FIFO
        # offset bug.  The old b'\x00' dummy byte was actually
        # being sent over the air as a valid RNode header (seq=0,
        # no split).  Now we send a proper header instead.
        header = os.urandom(1)[0] & _SEQ_MASK

        try:
            if len(data) > _FRAME_PAYLOAD:
                # Split into 2 frames (RNode protocol). LBT gates each frame
                # separately, so the second half defers while e.g. a repeater
                # is still re-transmitting the first.
                header |= _FLAG_SPLIT
                hdr = bytes([header])
                self._send_frame(hdr + data[:_FRAME_PAYLOAD])
                self._send_frame(hdr + data[_FRAME_PAYLOAD:])
                log("LoRa TX " + str(len(data)) + "B split seq=" + hex(header >> 4), LOG_DEBUG)
            else:
                # Single frame
                self._send_frame(bytes([header]) + data)
                log("LoRa TX " + str(len(data)) + "B", LOG_DEBUG)

            self.txb += len(data)
            self.tx += 1
            self._last_activity = time.time()
            return True
        except Exception as e:
            log("LoRa send error: " + str(e), LOG_ERROR)
            self._acquire()
            try:
                self._modem.start_recv(continuous=True)
            except:
                pass
            self._release()
            return False

    def _rx_frame(self, raw):
        """Parse one received LoRa frame (RNode framing: raw[0] = header,
        upper nibble seq, bit 0 split). Returns a complete packet's payload
        bytes, or None while a split is pending / the frame is dropped.

        Hardened against transparent repeaters that re-transmit every frame
        verbatim: without these checks a copied frame 1 (same seq) would be
        appended as "frame 2", corrupting every split packet — i.e. every
        Resource part >254B (camera images, large LXMF). Transport dedup
        can't help because reassembly sits below the packet layer."""
        if len(raw) < 2:
            return None

        header = raw[0]
        payload = raw[1:]
        if not (header & _FLAG_SPLIT):
            return payload

        seq = header & _SEQ_MASK
        now = time.time()

        # Verbatim copy of a half of the split we just completed -> drop.
        d = self._reasm_done
        if d and d[0] == seq and now - d[5] < _REASM_DUP_WINDOW:
            if ((len(payload) == d[1] and payload[:6] == d[2])
                    or (len(payload) == d[3] and payload[:6] == d[4])):
                log("LoRa split dup (completed pkt), dropped", LOG_DEBUG)
                return None

        if self._reasm_buf is None or self._reasm_seq != seq:
            # First fragment (or new seq replaces stale one)
            if self._reasm_buf is not None:
                log("LoRa split seq mismatch, restarting", LOG_DEBUG)
                self._reasm_buf = None
                self._reasm_seq = None
            # Frame 1 of a split is always full-size (254B payload) per the
            # RNode protocol; anything shorter seen first is a stray second
            # half (missed frame 1, or a late repeater copy) -> drop.
            if len(payload) < _FRAME_PAYLOAD:
                log("LoRa stray split tail dropped (" + str(len(payload)) + "B)", LOG_DEBUG)
                return None
            self._reasm_buf = bytearray(payload)
            self._reasm_seq = seq
            self._reasm_time = now
            log("LoRa split frame 1: " + str(len(payload)) + "B seq=" + hex(seq >> 4), LOG_DEBUG)
            return None

        # Pending reassembly, matching seq. An identical fragment is a
        # verbatim copy of frame 1 (repeater) -> drop, keep waiting.
        if len(payload) == len(self._reasm_buf) and payload == self._reasm_buf:
            log("LoRa split dup frame 1, dropped", LOG_DEBUG)
            return None

        # Genuine second fragment — complete, and fingerprint both halves.
        len1 = len(self._reasm_buf)
        self._reasm_buf.extend(payload)
        pkt = bytes(self._reasm_buf)
        self._reasm_done = (seq, len1, pkt[:6], len(payload), bytes(payload[:6]), now)
        self._reasm_buf = None
        self._reasm_seq = None
        log("LoRa split frame 2: " + str(len(payload)) + "B -> " + str(len(pkt)) + "B total", LOG_DEBUG)
        return pkt

    async def poll_loop(self):
        import uasyncio as asyncio

        log("LoRa poll loop started for " + self.name, LOG_NOTICE)

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
                        + " crc_err=" + str(_crc_errs)
                        + " lbt=" + str(self._lbt_waits)
                        + "/" + str(self._lbt_forced), LOG_DEBUG)
                    _rx_true_count = 0
                    _rx_pkt_count = 0
                    _last_diag = now

                # Stale reassembly cleanup
                if (self._reasm_buf is not None
                        and now - self._reasm_time > _REASM_TIMEOUT):
                    log("LoRa discarding stale split fragment", LOG_DEBUG)
                    self._reasm_buf = None
                    self._reasm_seq = None

                self._acquire()
                rx = self._modem.poll_recv()
                self._release()

                if rx is False:
                    log("LoRa modem stopped receiving, restarting", LOG_ERROR)
                    self._acquire()
                    self._modem.start_recv(continuous=True)
                    self._release()

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

                    if hasattr(rx, "valid_crc") and not rx.valid_crc:
                        log("LoRa CRC fail, discarding", LOG_DEBUG)
                        await asyncio.sleep(0.05)
                        continue

                    # raw[0] is the RNode header byte.  The lora-sx126x
                    # driver returns the exact bytes received over the air
                    # — there is no FIFO offset bug.  (The old raw[1:]
                    # "spurious byte strip" was actually stripping the
                    # RNode header, which happened to work for non-split
                    # packets.)
                    pkt = self._rx_frame(raw)

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
