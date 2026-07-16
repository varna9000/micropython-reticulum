# µReticulum Buffer
# Reliable bidirectional byte streams over a Channel — port of upstream
# RNS/Buffer.py, adapted for MicroPython + ESP32-S3 + the urns cooperative
# (uasyncio) event loop. Wire-compatible with reference RNS (SMT_STREAM_DATA).
#
# MicroPython/ESP32 adaptations vs upstream (see the port plan):
#   * no io.RawIOBase/BufferedReader/Writer/RWPair — plain classes; the Buffer
#     factories return the Raw reader/writer directly.
#   * no threads/locks — ready-callbacks are called SYNCHRONOUSLY on the event
#     loop (must be light; schedule async work). Upstream spawned a thread per
#     callback — wrong for ESP32.
#   * bz2 via urns.bz2dec (native fast path; compress() may return None -> send
#     uncompressed). Decompress is bounded to MAX_CHUNK_LEN.
#   * writer.write() is NON-BLOCKING: returns bytes accepted, possibly 0 when
#     the channel is full (every send over LoRa window=1 while a packet is
#     unacked). Callers MUST `await` between retries — a synchronous spin
#     starves the loop so the delivery proof never lands (permanent deadlock).
#     close() fires EOF best-effort; use `await aclose()` to drain+EOF reliably.
#   * bytearray ops use reassignment / same-length slice-assign only (del/resize
#     slice ops are unreliable across MicroPython versions).

import struct
from . import const
from . import bz2dec
from .link import _link_mdu
from .channel import (MessageBase, SystemMessageTypes, ChannelException,
                      ME_LINK_NOT_READY)
from .log import log, LOG_ERROR

_OVERHEAD = 2 + 6   # 2-byte stream header + 6-byte channel envelope header
_MAX_DATA_LEN = _link_mdu(const.MTU) - _OVERHEAD   # 423 at MTU 500 (interop default)


class StreamDataMessage(MessageBase):
    """A chunk of a byte stream, addressed to a stream id. System message type,
    wire-identical to upstream RNS.Buffer.StreamDataMessage."""

    MSGTYPE       = SystemMessageTypes.SMT_STREAM_DATA   # 0xff00
    STREAM_ID_MAX = 0x3fff                               # 14-bit id
    OVERHEAD      = _OVERHEAD
    MAX_DATA_LEN  = _MAX_DATA_LEN
    MAX_CHUNK_LEN = 1024 * 16

    def __init__(self, stream_id=None, data=None, eof=False, compressed=False):
        if stream_id is not None and stream_id > self.STREAM_ID_MAX:
            raise ValueError("stream_id must be 0-16383")
        self.stream_id = stream_id
        self.compressed = compressed
        self.data = data if data is not None else b""
        self.eof = eof

    def pack(self):
        if self.stream_id is None:
            raise ValueError("stream_id")
        header = ((0x3fff & self.stream_id)
                  | (0x8000 if self.eof else 0x0000)
                  | (0x4000 if self.compressed else 0x0000))
        return struct.pack(">H", header) + (self.data if self.data else b"")

    def unpack(self, raw):
        header = struct.unpack(">H", raw[:2])[0]
        self.eof = (0x8000 & header) > 0
        self.compressed = (0x4000 & header) > 0
        self.stream_id = header & 0x3fff
        self.data = raw[2:]
        if self.compressed and self.data:
            out = bz2dec.decompress(bytes(self.data))
            if out is None:
                raise OSError("bz2 decompress failed")
            if len(out) > self.MAX_CHUNK_LEN:
                raise OSError("decompressed chunk exceeds maximum legitimate size")
            self.data = out
            self.compressed = False


class RawChannelReader:
    """Receives one stream id off a Channel. Accumulates incoming bytes; the
    consumer drains them with read()/readinto(). Ready-callbacks fire (sync)
    when new data arrives. No true backpressure — the Channel ACKs on receive
    regardless of reads, so a stalled consumer grows the buffer (bounded warn)."""

    def __init__(self, stream_id, channel, max_buffer=128 * 1024):
        self._stream_id = stream_id
        self._channel = channel
        self._buffer = bytearray()
        self._eof = False
        self._max_buffer = max_buffer
        self._warned = False
        self._listeners = []
        channel._register_message_type(StreamDataMessage, is_system_type=True)
        channel.add_message_handler(self._handle_message)

    def add_ready_callback(self, cb):
        if cb not in self._listeners:
            self._listeners.append(cb)

    def remove_ready_callback(self, cb):
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _handle_message(self, message):
        if isinstance(message, StreamDataMessage) and message.stream_id == self._stream_id:
            if message.data:
                self._buffer.extend(message.data)
                if len(self._buffer) > self._max_buffer and not self._warned:
                    self._warned = True
                    log("Buffer reader " + str(self._stream_id) + " exceeded "
                        + str(self._max_buffer) + "B (no backpressure; consumer stalled?)", LOG_ERROR)
            if message.eof:
                self._eof = True
            ready = len(self._buffer)
            for cb in list(self._listeners):   # copy: a callback may add/remove
                try:
                    cb(ready)
                except Exception as e:
                    log("Buffer ready-callback error on stream "
                        + str(self._stream_id) + ": " + str(e), LOG_ERROR)
            return True
        return False

    def _read(self, size):
        r = self._buffer[:size]
        self._buffer = self._buffer[size:]   # reassign (MicroPython-safe)
        return r if len(r) > 0 or self._eof else None

    def read(self, size=-1):
        """Return up to `size` buffered bytes (all available when size < 0).
        Non-blocking: returns b'' when nothing is available. Check .eof for end."""
        if size is None or size < 0:
            size = len(self._buffer)
        r = self._read(size)
        return bytes(r) if r is not None else b""

    def readinto(self, buf):
        """Fill `buf`; return bytes read, or None when nothing is available."""
        r = self._read(len(buf))
        if r is None:
            return None
        n = len(r)
        buf[:n] = r        # same-length slice-assign (verified on MicroPython v1.24)
        return n

    @property
    def eof(self):
        return self._eof

    def readable(self):
        return True

    def writable(self):
        return False

    def close(self):
        try:
            self._channel.remove_message_handler(self._handle_message)
        except Exception:
            pass
        self._listeners = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


class RawChannelWriter:
    """Sends one stream id over a Channel, chunking + adaptively bz2-compressing.
    write() is non-blocking (returns bytes accepted, 0 when the channel is full)."""

    MAX_CHUNK_LEN     = 1024 * 16
    COMPRESSION_TRIES = 4

    def __init__(self, stream_id, channel):
        self._stream_id = stream_id
        self._channel = channel
        self._eof = False
        # Max data bytes per packet: channel.mdu already excludes the 6-byte
        # envelope, so subtract only the 2-byte stream header. = 423 at MTU 500.
        self._max_data = max(1, channel.mdu - 2)

    def write(self, b):
        try:
            comp_tries = RawChannelWriter.COMPRESSION_TRIES
            comp_try = 1
            comp_success = False
            chunk_len = len(b)
            if chunk_len > RawChannelWriter.MAX_CHUNK_LEN:
                chunk_len = RawChannelWriter.MAX_CHUNK_LEN
                b = b[:RawChannelWriter.MAX_CHUNK_LEN]
            compressed_chunk = None
            seg_len = chunk_len
            # Try to compress a leading segment small enough to fit one packet.
            # bz2dec.compress may return None (no native compress) -> uncompressed.
            while chunk_len > 32 and comp_try < comp_tries:
                seg_len = int(chunk_len / comp_try)
                c = bz2dec.compress(bytes(b[:seg_len]))
                if c is not None and len(c) < self._max_data and len(c) < seg_len:
                    compressed_chunk = c
                    comp_success = True
                    break
                comp_try += 1

            if comp_success:
                chunk = compressed_chunk
                processed = seg_len
            else:
                chunk = bytes(b[:self._max_data])
                processed = len(chunk)

            self._channel.send(StreamDataMessage(self._stream_id, chunk, self._eof, comp_success))
            return processed

        except ChannelException as cex:
            # Channel full (window/receipts): report 0 bytes accepted so the
            # caller retries AFTER yielding. Any other channel error (e.g.
            # ME_TOO_BIG) is a real bug — surface it.
            if cex.type != ME_LINK_NOT_READY:
                raise
            return 0

    def close(self):
        """Best-effort EOF (fire-and-forget; may not send if the channel is
        full). Prefer `await aclose()` for reliable end-of-stream."""
        self._eof = True
        try:
            if self._channel.is_ready_to_send():
                self._channel.send(StreamDataMessage(self._stream_id, b"", True, False))
        except Exception:
            pass

    async def aclose(self):
        """Drain then send EOF, yielding to the event loop (never blocks)."""
        import uasyncio as asyncio
        self._eof = True
        for _ in range(300):   # ~15 s at 50 ms
            try:
                if self._channel.is_ready_to_send():
                    self._channel.send(StreamDataMessage(self._stream_id, b"", True, False))
                    return
            except ChannelException:
                pass
            await asyncio.sleep_ms(50)

    def readable(self):
        return False

    def writable(self):
        return True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


class _BidirectionalBuffer:
    """A reader+writer pair over one channel (different stream ids)."""

    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer

    def read(self, size=-1):
        return self._reader.read(size)

    def readinto(self, buf):
        return self._reader.readinto(buf)

    def write(self, b):
        return self._writer.write(b)

    def add_ready_callback(self, cb):
        self._reader.add_ready_callback(cb)

    def remove_ready_callback(self, cb):
        self._reader.remove_ready_callback(cb)

    @property
    def eof(self):
        return self._reader.eof

    def close(self):
        self._writer.close()
        self._reader.close()

    async def aclose(self):
        await self._writer.aclose()
        self._reader.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


class Buffer:
    """Factories for byte streams over a Channel. Unlike upstream (which wraps
    the raw objects in io.BufferedReader/Writer/RWPair — absent in MicroPython),
    these return the Raw reader/writer directly: use .read(n)/.readinto(buf)/
    .write(b)/ready-callbacks."""

    @staticmethod
    def create_reader(stream_id, channel, ready_callback=None):
        reader = RawChannelReader(stream_id, channel)
        if ready_callback:
            reader.add_ready_callback(ready_callback)
        return reader

    @staticmethod
    def create_writer(stream_id, channel):
        return RawChannelWriter(stream_id, channel)

    @staticmethod
    def create_bidirectional_buffer(receive_stream_id, send_stream_id, channel, ready_callback=None):
        reader = RawChannelReader(receive_stream_id, channel)
        if ready_callback:
            reader.add_ready_callback(ready_callback)
        writer = RawChannelWriter(send_stream_id, channel)
        return _BidirectionalBuffer(reader, writer)
