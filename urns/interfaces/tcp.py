# µReticulum TCP Client Interface
# HDLC-framed TCP connection to a remote RNS TCPServerInterface

import time
import socket
from . import Interface
from ..log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE

# HDLC framing constants (inlined to keep module self-contained)
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


class TCPClientInterface(Interface):
    HW_MTU = 16384
    CONNECT_TIMEOUT = 5
    RECONNECT_WAIT = 5
    MAX_RECONNECTS = 0       # 0 = unlimited

    def __init__(self, config):
        name = config.get("name", "TCP")
        super().__init__(name)

        self.target_host = config.get("target_host", "localhost")
        self.target_port = config.get("target_port", 4242)
        self.reconnect_wait = config.get("reconnect_wait", self.RECONNECT_WAIT)
        self.max_reconnects = config.get("max_reconnects", self.MAX_RECONNECTS)

        self._socket = None
        self._in_frame = False
        self._escape = False
        self._buffer = bytearray()
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
        self._in_frame = False
        self._escape = False
        self._buffer = bytearray()
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
            # Wrap HDR_1 packets as HDR_2 TRANSPORT when the destination
            # hash is in path_table (known via announce). Only TCP needs
            # this — broadcast interfaces (UDP, LoRa) send HDR_1 directly.
            # Link-addressed packets (link_id as dest) are NOT wrapped —
            # the transport server routes these via its link_table.
            if len(data) >= 19 and (data[0] & 0x40) == 0x00 and (data[0] & 0x03) != 0x01:
                from ..transport import Transport
                transport_id = Transport.path_table.get(data[2:18])
                if transport_id:
                    # Set HDR_2 (bit 6) + TRANSPORT (bit 4), keep other bits
                    data = bytes([data[0] | 0x50]) + data[1:2] + transport_id + data[2:]

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

    def _process_byte(self, byte):
        if self._in_frame and byte == FLAG:
            self._in_frame = False
            if len(self._buffer) > 0:
                raw = bytes(self._buffer)
                if len(raw) >= 18:
                    log("TCP RX " + str(len(raw)) + "B flags=0x" + ("%02x" % raw[0]) + " dest=" + raw[2:18].hex(), LOG_DEBUG)
                else:
                    log("TCP RX " + str(len(raw)) + "B", LOG_DEBUG)
                self.process_incoming(raw)
                self._buffer = bytearray()

        elif byte == FLAG:
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

    async def poll_loop(self):
        import uasyncio as asyncio

        log("TCP poll loop started for " + self.name, LOG_VERBOSE)

        while self.enabled:
            if not self.online:
                self._reconnect()
                await asyncio.sleep(1)
                continue

            try:
                # Re-assert non-blocking before every recv — ESP32 lwIP
                # bug: send() corrupts the socket's non-blocking state.
                # process_outgoing() restores it after sendall(), but
                # guard here too in case of any edge cases.
                self._socket.settimeout(0)
                data = self._socket.recv(4096)
                if data:
                    for b in data:
                        self._process_byte(b)
                else:
                    # Empty recv = connection closed
                    log("TCP connection closed by remote", LOG_NOTICE)
                    self.online = False
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

            await asyncio.sleep(0.01)

        log("TCP poll loop EXITED for " + self.name, LOG_ERROR)

    def close(self):
        super().close()
        self._close_socket()
        log("TCP Interface " + self.name + " closed", LOG_VERBOSE)

    def __str__(self):
        return "TCPClientInterface[" + self.name + "]"
