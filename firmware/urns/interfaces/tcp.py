# µReticulum TCP Client Interface
# HDLC-framed TCP connection to a remote RNS TCPServerInterface

import time
import socket
from . import Interface
from ..log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE

# HDLC framing constants (inlined to keep module self-contained)
MIN_FRAME_LEN = 19   # == const.HEADER_MINSIZE; shorter cannot be a packet
FLAG     = 0x7E
ESC      = 0x7D
ESC_MASK = 0x20
_FLAG_BYTES = b"\x7e"

# Millisecond monotonic clock: MicroPython ticks_ms, host-CPython fallback.
_ticks_ms = getattr(time, "ticks_ms", None) or (lambda: int(time.time() * 1000))
_ticks_diff = getattr(time, "ticks_diff", None) or (lambda a, b: a - b)


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


class TCPClientInterface(Interface):
    # Exactly one peer on the far end, so an announce received here must not be
    # echoed back to it (see Transport._rebroadcast_announce).
    POINT_TO_POINT = True
    HW_MTU = 16384
    CONNECT_TIMEOUT = 5
    RECONNECT_WAIT = 5
    MAX_RECONNECTS = 0       # 0 = unlimited

    # Per-iteration drain bounds so cohabiting tasks (GUI, LoRa) still get
    # scheduled during a bulk transfer: RX_BUDGET caps bytes, RX_SLICE_MS
    # caps wall time (frame processing runs inline, and a burst of small
    # frames costs far more time than its byte count suggests — one stalled
    # iteration above ~2 GUI frames reads as scroll jank). Un-drained bytes
    # stay in the socket buffer for the next iteration; nothing is lost.
    RX_BUDGET = 16384
    RX_SLICE_MS = 25

    def __init__(self, config):
        name = config.get("name", "TCP")
        super().__init__(name)

        self.target_host = config.get("target_host", "localhost")
        self.target_port = config.get("target_port", 4242)
        self.reconnect_wait = config.get("reconnect_wait", self.RECONNECT_WAIT)
        self.max_reconnects = config.get("max_reconnects", self.MAX_RECONNECTS)

        self._socket = None
        self._frame = None            # None = outside frame, else escaped bytes so far
        self._frame_overflow = False
        self._recv_buf = bytearray(2048)
        self._recv_mv = memoryview(self._recv_buf)
        self._reconnect_count = 0
        self._last_reconnect = 0

        try:
            self._connect()
        except Exception as e:
            log("TCP initial connect failed: " + str(e), LOG_ERROR)

    def _connect(self):
        addr_info = socket.getaddrinfo(self.target_host, self.target_port)
        addr = addr_info[0][-1]

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.CONNECT_TIMEOUT)
        s.connect(addr)
        s.settimeout(0)

        try:
            s.setsockopt(socket.IPPROTO_TCP, 1, 1)  # TCP_NODELAY = 1
        except:
            pass

        self._socket = s
        self._frame = None
        self._frame_overflow = False
        self.online = True
        self._reconnect_count = 0
        log("TCP connected to " + self.target_host + ":" + str(self.target_port), LOG_NOTICE)

    def _close_socket(self):
        if self._socket:
            try:
                self._socket.close()
            except:
                pass
            self._socket = None

    def _reconnect(self):
        now = time.time()
        if now - self._last_reconnect < self.reconnect_wait:
            return
        self._last_reconnect = now

        if self.max_reconnects > 0 and self._reconnect_count >= self.max_reconnects:
            log("TCP max reconnect attempts reached", LOG_ERROR)
            self.enabled = False
            return

        self._reconnect_count += 1
        log("TCP reconnecting (" + str(self._reconnect_count) + ")...", LOG_NOTICE)
        self._close_socket()

        try:
            self._connect()
        except Exception as e:
            log("TCP reconnect failed: " + str(e), LOG_ERROR)

    def process_outgoing(self, data):
        if not self.online or not self._socket:
            return False

        try:
            # Wrap HDR_1 packets as HDR_2 TRANSPORT when the destination is
            # more than one hop away. Only TCP needs this — broadcast
            # interfaces (UDP, LoRa) send HDR_1 directly.
            # Link-addressed packets (link_id as dest) are NOT wrapped —
            # the transport server routes these via its link_table.
            #
            # The hop check is load-bearing, and mirrors reference RNS
            # Transport.outbound: transport headers are inserted only when
            # hops > 1. A destination one hop away is reachable directly on
            # this interface, so it must go out as plain HDR_1 — even though
            # its path entry's next_hop is a transport node's identity rather
            # than the destination itself. That is the case for EVERY
            # destination behind a shared instance (rnsh, nomadnet et al.
            # behind rnsd), which is exactly when this used to misfire.
            #
            # Wrapping such a send addresses it "via <transport>"; the shared
            # instance hands it to its local client with the transport header
            # still attached (stripping is gated on the local_hops_delta
            # privacy feature, which is off by default), and the client drops
            # it, because Transport.inbound only accepts a LINKREQUEST whose
            # transport_id is None or equals its OWN transport identity — and
            # a shared-instance client generates an ephemeral identity that
            # never matches the instance's. Result: the link request dies
            # silently, no LRPROOF, nothing logged at default verbosity.
            #
            # (Reference RNS also upgrades at hops == 1 when the sending node
            # is ITSELF a shared-instance client, to push the packet through
            # its own instance. urns is always standalone —
            # Reticulum.is_connected_to_shared_instance is hardcoded False —
            # so that case does not apply here.)
            if len(data) >= 19 and (data[0] & 0x40) == 0x00 and (data[0] & 0x03) != 0x01:
                from ..transport import Transport
                from .. import const
                _entry = Transport.path_table.get(data[2:18])
                if _entry and _entry[const.IDX_PT_HOPS] > 1:
                    # Set HDR_2 (bit 6) + TRANSPORT (bit 4), keep other bits
                    transport_id = _entry[const.IDX_PT_NEXT_HOP]
                    data = bytes([data[0] | 0x50]) + data[1:2] + transport_id + data[2:]

            # Apply IFAC after transport wrapping, before framing
            data = self.ifac_sign(data)

            frame = bytes([FLAG]) + hdlc_escape(data) + bytes([FLAG])
            # Switch to blocking mode with timeout for reliable sendall().
            # MicroPython ESP32 lwIP: sendall() on non-blocking sockets
            # may silently truncate data if EAGAIN occurs mid-send.
            self._socket.settimeout(2)
            self._socket.sendall(frame)
            # Restore non-blocking for poll_loop recv
            self._socket.settimeout(0)
            self.txb += len(data)
            self.tx += 1
            self._last_activity = time.time()
            if len(data) >= 18:
                log("TCP TX " + str(len(data)) + "B frame=" + str(len(frame)) + "B flags=0x" + ("%02x" % data[0]) + " dest=" + data[2:18].hex(), LOG_DEBUG)
            return True
        except Exception as e:
            log("TCP send error: " + str(e), LOG_ERROR)
            try:
                self._socket.settimeout(0)
            except:
                pass
            self.online = False
            return False

    def _feed(self, data):
        """Chunk-level HDLC deframer. Replaces the former per-byte state
        machine: a Python-level call per byte cost ~32us/byte on ESP32-S3
        (~30KB/s CPU ceiling); scanning for FLAG with bytes.find() and
        unescaping with two replace() passes runs at C speed.

        Semantics match the byte machine: a FLAG opens a frame, the next FLAG
        closes and delivers it, bytes outside a frame are discarded (RNS HDLC
        brackets every frame with its own leading and trailing FLAG, so
        between-frame bytes only occur when joining a stream mid-frame)."""
        data = bytes(data)
        pos = 0
        n = len(data)
        while pos < n:
            idx = data.find(_FLAG_BYTES, pos)
            if self._frame is None:
                if idx < 0:
                    return                      # garbage before any FLAG
                self._frame = bytearray()       # frame opens
                pos = idx + 1
            elif idx < 0:
                # Frame continues past this chunk. Cap the escaped size at
                # 2x HW_MTU (worst case every byte escaped) — beyond that the
                # frame can only be truncation or stream corruption.
                if len(self._frame) + (n - pos) > 2 * self.HW_MTU + 2:
                    self._frame_overflow = True
                else:
                    self._frame += data[pos:]
                return
            else:
                if len(self._frame) + (idx - pos) > 2 * self.HW_MTU + 2:
                    self._frame_overflow = True
                else:
                    self._frame += data[pos:idx]
                self._deliver_frame()
                pos = idx + 1

    def _deliver_frame(self):
        esc = self._frame
        self._frame = None
        overflow = self._frame_overflow
        self._frame_overflow = False
        if len(esc) == 0:
            return                              # back-to-back FLAGs
        # Unescape at C speed. Pass order is safe: any 0x7D in the escaped
        # stream is an escape lead, so 0x7D 0x5E is always an escaped FLAG;
        # the second pass then resolves the remaining 0x7D 0x5D pairs.
        raw = bytes(esc).replace(b"\x7d\x5e", b"\x7e").replace(b"\x7d\x5d", b"\x7d")
        # Validate before handing anything to the transport layer. A frame
        # shorter than a header cannot be a packet, and one that overflowed
        # HW_MTU was truncated mid-flight — delivering it feeds a corrupt
        # packet into routing (reference RNS 1.3.9 added the same length
        # checks to its HDLC reader).
        if overflow or len(raw) > self.HW_MTU:
            log("TCP oversized HDLC frame dropped (>" + str(self.HW_MTU) + "B)", LOG_DEBUG)
        elif len(raw) <= MIN_FRAME_LEN:
            log("TCP undersized HDLC frame dropped (" + str(len(raw)) + "B)", LOG_DEBUG)
        else:
            log("TCP RX " + str(len(raw)) + "B flags=0x" + ("%02x" % raw[0]) + " dest=" + raw[2:18].hex(), LOG_DEBUG)
            self.process_incoming(raw)

    async def poll_loop(self):
        import uasyncio as asyncio

        log("TCP poll loop started for " + self.name, LOG_VERBOSE)

        while self.enabled:
            if not self.online:
                self._reconnect()
                await asyncio.sleep(1)
                continue

            got = 0
            t0 = _ticks_ms()
            try:
                # Re-assert non-blocking before every drain — ESP32 lwIP
                # bug: send() corrupts the socket's non-blocking state.
                # process_outgoing() restores it after sendall(), but
                # guard here too in case of any edge cases.
                self._socket.settimeout(0)
                # Drain until EAGAIN, byte budget or time slice. The former
                # single 512B read per 10ms tick capped throughput at ~50KB/s
                # before any processing cost; a busy hub link needs the
                # backlog cleared in bursts, with the bounds capping loop
                # monopolization.
                while got < self.RX_BUDGET:
                    n = self._socket.readinto(self._recv_buf)
                    if n is None:
                        break                   # EAGAIN surfaced as None
                    if n == 0:
                        log("TCP connection closed by remote", LOG_NOTICE)
                        self.online = False
                        break
                    got += n
                    self._feed(self._recv_mv[:n])
                    if _ticks_diff(_ticks_ms(), t0) >= self.RX_SLICE_MS:
                        break
            except OSError as e:
                if e.args[0] == 11:  # EAGAIN
                    pass
                else:
                    log("TCP recv error: " + str(e), LOG_ERROR)
                    self.online = False
            except Exception as e:
                # Catch non-OSError exceptions from the deep call chain
                # (process_incoming → Transport.inbound → decrypt →
                # packet.prove → process_outgoing) to prevent the poll
                # loop from crashing.
                log("TCP poll error: " + str(e), LOG_ERROR)

            if got and (got >= self.RX_BUDGET
                        or _ticks_diff(_ticks_ms(), t0) >= self.RX_SLICE_MS):
                await asyncio.sleep(0)          # backlog likely: yield only
            elif got:
                await asyncio.sleep(0.002)
            else:
                await asyncio.sleep(0.01)

        log("TCP poll loop EXITED for " + self.name, LOG_ERROR)

    def close(self):
        super().close()
        self._close_socket()
        log("TCP Interface " + self.name + " closed", LOG_VERBOSE)

    def __str__(self):
        return "TCPClientInterface[" + self.name + "]"
