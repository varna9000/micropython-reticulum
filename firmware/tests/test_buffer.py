# Host-side tests for urns/buffer.py — StreamDataMessage framing, RawChannelReader
# accumulation + read/readinto + synchronous ready-callbacks, RawChannelWriter
# chunking + non-blocking (0-on-not-ready) + EOF, and the bidirectional buffer.
# Uses a REAL urns.channel.Channel over a mock outlet (exercises registration +
# _receive + send). Run:  python3 firmware/tests/test_buffer.py

import os
import sys
import struct
import bz2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401

import importlib
channel = importlib.import_module("urns.channel")
buffer = importlib.import_module("urns.buffer")

SDM = buffer.StreamDataMessage


# --- mock outlet (records sent envelopes; controllable delivery) ------------
class MockReceipt:
    def __init__(self):
        self.timeout = 1.0
    def set_timeout(self, t):
        self.timeout = t


class MockPacket:
    _n = 0
    def __init__(self, raw):
        MockPacket._n += 1
        self.id = MockPacket._n
        self.raw = raw
        self.state = channel.MSGSTATE_SENT
        self.receipt = MockReceipt()
        self.dcb = None
        self.tcb = None


class MockOutlet:
    def __init__(self, rtt=0.1, mdu=431):
        self._rtt = rtt
        self._mdu = mdu
        self.usable = True
        self.sent = []
    def send(self, raw):
        p = MockPacket(raw)
        self.sent.append(p)
        return p
    def resend(self, packet):
        p = MockPacket(packet.raw)
        self.sent.append(p)
        return p
    @property
    def mdu(self):
        return self._mdu
    @property
    def rtt(self):
        return self._rtt
    @property
    def is_usable(self):
        return self.usable
    def get_packet_state(self, packet):
        return packet.state
    def timed_out(self):
        pass
    def get_packet_id(self, packet):
        return packet.id
    def set_packet_timeout_callback(self, packet, cb, timeout=None):
        packet.tcb = cb
    def set_packet_delivered_callback(self, packet, cb):
        packet.dcb = cb
    def deliver(self, packet):
        packet.state = channel.MSGSTATE_DELIVERED
        if packet.dcb:
            packet.dcb(packet)


def _inject(ch, sdm, seq):
    """Feed a StreamDataMessage into a real Channel as if received."""
    ch._receive(channel.Envelope(None, message=sdm, sequence=seq).pack())


def _last_sent_sdm(outlet):
    """Parse the last envelope the outlet sent back into a StreamDataMessage."""
    env = channel.Envelope(None, raw=outlet.sent[-1].raw)
    return env.unpack({SDM.MSGTYPE: SDM})


# --- StreamDataMessage framing ---------------------------------------------
def test_streamdata_framing():
    m = SDM(5, b"hello")
    raw = m.pack()
    assert struct.unpack(">H", raw[:2])[0] == 5 and raw[2:] == b"hello"
    m2 = SDM(); m2.unpack(raw)
    assert m2.stream_id == 5 and m2.data == b"hello" and not m2.eof and not m2.compressed

    eofm = SDM(1, b"", eof=True)
    assert struct.unpack(">H", eofm.pack()[:2])[0] == (0x8000 | 1)
    assert SDM.MSGTYPE == 0xff00 and SDM.MAX_DATA_LEN == 423
    print("ok test_streamdata_framing")


def test_streamdata_compressed_receive():
    payload = b"drwxr-xr-x  6 user staff  192 main.py\n" * 30
    comp = bz2.compress(payload)
    raw = struct.pack(">H", 0x4000 | 7) + comp   # compressed flag, stream 7
    m = SDM(); m.unpack(raw)
    assert m.stream_id == 7 and m.data == payload and m.compressed is False
    print("ok test_streamdata_compressed_receive")


# --- reader -----------------------------------------------------------------
def test_reader_accumulate_and_eof():
    ch = channel.Channel(MockOutlet(rtt=0.1))
    r = buffer.RawChannelReader(3, ch)
    _inject(ch, SDM(3, b"abc"), 0)
    _inject(ch, SDM(3, b"def"), 1)
    assert not r.eof
    _inject(ch, SDM(3, b"!", eof=True), 2)
    assert r.eof
    assert r.read() == b"abcdef!"
    assert r.read() == b""    # drained
    print("ok test_reader_accumulate_and_eof")


def test_reader_wrong_stream_ignored():
    ch = channel.Channel(MockOutlet(rtt=0.1))
    r = buffer.RawChannelReader(1, ch)
    _inject(ch, SDM(2, b"nope"), 0)   # different stream id
    _inject(ch, SDM(1, b"yes"), 1)
    assert r.read() == b"yes"
    print("ok test_reader_wrong_stream_ignored")


def test_reader_ready_callback_sync():
    ch = channel.Channel(MockOutlet(rtt=0.1))
    r = buffer.RawChannelReader(0, ch)
    seen = []
    r.add_ready_callback(lambda n: seen.append(n))
    _inject(ch, SDM(0, b"12345"), 0)
    assert seen == [5]                # fired synchronously with byte count
    _inject(ch, SDM(0, b"67"), 1)
    assert seen == [5, 7]
    print("ok test_reader_ready_callback_sync")


def test_reader_readinto():
    ch = channel.Channel(MockOutlet(rtt=0.1))
    r = buffer.RawChannelReader(0, ch)
    _inject(ch, SDM(0, b"ABCD"), 0)
    buf = bytearray(8)
    n = r.readinto(buf)
    assert n == 4 and bytes(buf) == b"ABCD\x00\x00\x00\x00"
    assert r.readinto(bytearray(4)) is None   # nothing left, no eof
    print("ok test_reader_readinto")


def test_reader_close_removes_handler():
    ch = channel.Channel(MockOutlet(rtt=0.1))
    r = buffer.RawChannelReader(0, ch)
    assert len(ch._message_callbacks) == 1
    r.close()
    assert ch._message_callbacks == []
    print("ok test_reader_close_removes_handler")


# --- writer -----------------------------------------------------------------
def test_writer_sends_uncompressed_chunk():
    outlet = MockOutlet(rtt=0.1, mdu=431)   # window 2
    ch = channel.Channel(outlet)
    w = buffer.RawChannelWriter(9, ch)
    assert w._max_data == 425 - 2          # channel.mdu(425) - 2 = 423
    n = w.write(b"x" * 1000)               # host has no native compress -> uncompressed
    assert n == 423, n
    m = _last_sent_sdm(outlet)
    assert m.stream_id == 9 and not m.compressed and len(m.data) == 423
    print("ok test_writer_sends_uncompressed_chunk")


def test_writer_not_ready_returns_zero():
    outlet = MockOutlet(rtt=2.0, mdu=431)   # rtt>1.45 -> window 1
    ch = channel.Channel(outlet)
    w = buffer.RawChannelWriter(0, ch)
    assert w.write(b"a" * 100) > 0          # first send ok
    assert w.write(b"b" * 100) == 0         # window full -> non-blocking 0
    outlet.deliver(outlet.sent[-1])         # ACK frees the window
    assert w.write(b"b" * 100) > 0          # now ok
    print("ok test_writer_not_ready_returns_zero")


def test_writer_close_sends_eof():
    outlet = MockOutlet(rtt=0.1, mdu=431)
    ch = channel.Channel(outlet)
    w = buffer.RawChannelWriter(4, ch)
    before = len(outlet.sent)
    w.close()
    assert len(outlet.sent) == before + 1
    m = _last_sent_sdm(outlet)
    assert m.stream_id == 4 and m.eof is True and m.data == b""
    print("ok test_writer_close_sends_eof")


def test_writer_reraises_non_not_ready():
    # write() must swallow ME_LINK_NOT_READY (return 0) but RE-RAISE any other
    # ChannelException (e.g. ME_TOO_BIG). (The writer's own bounding means
    # ME_TOO_BIG can't happen in normal use — the exact-fit is checked in
    # test_writer_sends_uncompressed_chunk — so force it at the channel.)
    ch = channel.Channel(MockOutlet(rtt=0.1, mdu=431))
    w = buffer.RawChannelWriter(0, ch)

    def too_big(msg):
        raise channel.ChannelException(channel.ME_TOO_BIG, "too big")
    ch.send = too_big
    raised = False
    try:
        w.write(b"abcd")
    except channel.ChannelException as e:
        raised = e.type == channel.ME_TOO_BIG
    assert raised, "writer must re-raise ME_TOO_BIG"

    def not_ready(msg):
        raise channel.ChannelException(channel.ME_LINK_NOT_READY, "nr")
    ch.send = not_ready
    assert w.write(b"abcd") == 0, "writer must swallow ME_LINK_NOT_READY -> 0"
    print("ok test_writer_reraises_non_not_ready")


# --- bidirectional + factories ---------------------------------------------
def test_bidirectional_and_factories():
    outlet = MockOutlet(rtt=0.1, mdu=431)
    ch = channel.Channel(outlet)
    seen = []
    bidi = buffer.Buffer.create_bidirectional_buffer(1, 2, ch, ready_callback=lambda n: seen.append(n))
    # send on stream 2
    n = bidi.write(b"ping")
    assert n == 4
    m = _last_sent_sdm(outlet)
    assert m.stream_id == 2 and m.data == b"ping"
    # receive on stream 1
    _inject(ch, SDM(1, b"pong"), 0)
    assert seen == [4] and bidi.read() == b"pong"

    assert isinstance(buffer.Buffer.create_reader(0, channel.Channel(MockOutlet())), buffer.RawChannelReader)
    assert isinstance(buffer.Buffer.create_writer(0, channel.Channel(MockOutlet())), buffer.RawChannelWriter)
    print("ok test_bidirectional_and_factories")


if __name__ == "__main__":
    for name in list(globals()):
        if name.startswith("test_"):
            globals()[name]()
    print("all buffer tests passed")
