# Host-side tests for urns/channel.py — Envelope framing, sequencing,
# windowing, and ACK/retransmit — using a mock outlet (no crypto needed).
# Run:  python3 firmware/tests/test_channel.py

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401  (installs shims + synthetic urns package)

import importlib
channel = importlib.import_module("urns.channel")

MSGSTATE_SENT = channel.MSGSTATE_SENT
MSGSTATE_DELIVERED = channel.MSGSTATE_DELIVERED


# --- test message ----------------------------------------------------------
class Ping(channel.MessageBase):
    MSGTYPE = 0x0101

    def __init__(self, data=b""):
        self.data = data

    def pack(self):
        return self.data

    def unpack(self, raw):
        self.data = raw


# --- mock outlet -----------------------------------------------------------
class MockReceipt:
    def __init__(self):
        self.timeout = 1.0

    def set_timeout(self, t):
        self.timeout = t


class MockPacket:
    _counter = 0

    def __init__(self, raw):
        MockPacket._counter += 1
        self.id = MockPacket._counter
        self.raw = raw
        self.state = MSGSTATE_SENT
        self.receipt = MockReceipt()
        self.delivered_cb = None
        self.timeout_cb = None


class MockOutlet:
    def __init__(self, rtt=0.1, mdu=419, usable=True):
        self._rtt = rtt
        self._mdu = mdu
        self.usable = usable
        self.sent = []          # every MockPacket sent (incl. resends)
        self.torn_down = False

    def send(self, raw):
        p = MockPacket(raw)
        self.sent.append(p)
        return p

    def resend(self, packet):
        p = MockPacket(packet.raw)   # emulate re-encrypt -> fresh packet
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
        self.torn_down = True

    def get_packet_id(self, packet):
        return packet.id

    def set_packet_timeout_callback(self, packet, cb, timeout=None):
        packet.timeout_cb = cb

    def set_packet_delivered_callback(self, packet, cb):
        packet.delivered_cb = cb

    # --- test drivers ---
    def deliver(self, packet):
        packet.state = MSGSTATE_DELIVERED
        if packet.delivered_cb:
            packet.delivered_cb(packet)

    def fire_timeout(self, packet):
        packet.state = channel.MSGSTATE_FAILED
        if packet.timeout_cb:
            packet.timeout_cb(packet)


def _raw(seq, data=b"x", outlet=None):
    env = channel.Envelope(outlet, message=Ping(data), sequence=seq)
    return env.pack()


# --- tests -----------------------------------------------------------------
def test_envelope_roundtrip():
    env = channel.Envelope(None, message=Ping(b"hello"), sequence=7)
    raw = env.pack()
    # 6-byte header: >HHH msgtype, seq, len
    import struct
    msgtype, seq, length = struct.unpack(">HHH", raw[:6])
    assert msgtype == Ping.MSGTYPE
    assert seq == 7
    assert length == 5
    env2 = channel.Envelope(None, raw=raw)
    msg = env2.unpack({Ping.MSGTYPE: Ping})
    assert env2.sequence == 7
    assert msg.data == b"hello"
    print("ok test_envelope_roundtrip")


def test_receive_in_order():
    ch = channel.Channel(MockOutlet(rtt=0.1))
    ch.register_message_type(Ping)
    got = []
    ch.add_message_handler(lambda m: got.append(m.data) or False)
    for s in range(3):
        ch._receive(_raw(s, bytes([0x40 + s])))
    assert got == [b"@", b"A", b"B"], got
    assert ch._next_rx_sequence == 3
    print("ok test_receive_in_order")


def test_receive_out_of_order():
    ch = channel.Channel(MockOutlet(rtt=0.1))
    ch.register_message_type(Ping)
    got = []
    ch.add_message_handler(lambda m: got.append(m.data) or False)
    ch._receive(_raw(0, b"a"))
    ch._receive(_raw(2, b"c"))   # gap — buffered, not delivered
    assert got == [b"a"], got
    ch._receive(_raw(1, b"b"))   # fills the gap -> delivers 1 then 2
    assert got == [b"a", b"b", b"c"], got
    print("ok test_receive_out_of_order")


def test_receive_duplicate():
    ch = channel.Channel(MockOutlet(rtt=0.1))
    ch.register_message_type(Ping)
    got = []
    ch.add_message_handler(lambda m: got.append(m.data) or False)
    ch._receive(_raw(0, b"a"))
    ch._receive(_raw(0, b"a"))   # duplicate sequence -> dropped
    ch._receive(_raw(1, b"b"))
    assert got == [b"a", b"b"], got
    print("ok test_receive_duplicate")


def test_window_slow_is_one():
    ch = channel.Channel(MockOutlet(rtt=2.0))   # > RTT_SLOW
    assert ch.window == 1 and ch.window_max == 1
    print("ok test_window_slow_is_one")


def test_window_gating_and_delivery():
    outlet = MockOutlet(rtt=2.0)   # window = 1
    ch = channel.Channel(outlet)
    ch.register_message_type(Ping)
    assert ch.is_ready_to_send()
    ch.send(Ping(b"one"))
    assert not ch.is_ready_to_send()      # one outstanding, window 1
    outlet.deliver(outlet.sent[-1])       # ACK it
    assert ch.is_ready_to_send()          # window frees up
    assert len(ch._tx_ring) == 0
    print("ok test_window_gating_and_delivery")


def test_resend_then_teardown():
    outlet = MockOutlet(rtt=2.0)   # window = 1
    ch = channel.Channel(outlet)
    ch.register_message_type(Ping)
    env = ch.send(Ping(b"z"))
    assert len(outlet.sent) == 1 and env.tries == 1
    # 4 timeouts -> 4 resends (fresh packets), tries climbs to 5
    for expected_sends in range(2, 6):
        outlet.fire_timeout(outlet.sent[-1])
        assert not outlet.torn_down
        assert len(outlet.sent) == expected_sends, (len(outlet.sent), expected_sends)
    assert env.tries == 5
    # 5th timeout: tries >= max_tries -> teardown, no further send
    outlet.fire_timeout(outlet.sent[-1])
    assert outlet.torn_down
    assert len(outlet.sent) == 5
    print("ok test_resend_then_teardown")


def test_delivery_stops_resend():
    outlet = MockOutlet(rtt=2.0)
    ch = channel.Channel(outlet)
    ch.register_message_type(Ping)
    ch.send(Ping(b"z"))
    outlet.deliver(outlet.sent[-1])
    # a stale timeout on a delivered packet must not resend or tear down
    outlet.fire_timeout(outlet.sent[-1])
    assert not outlet.torn_down
    assert len(outlet.sent) == 1
    print("ok test_delivery_stops_resend")


def test_mdu():
    ch = channel.Channel(MockOutlet(rtt=0.1, mdu=431))
    assert ch.mdu == 431 - 6
    print("ok test_mdu")


def test_system_type_registration():
    ch = channel.Channel(MockOutlet(rtt=0.1))

    class Sys(channel.MessageBase):
        MSGTYPE = channel.SystemMessageTypes.SMT_STREAM_DATA  # 0xff00
        def pack(self):
            return b""
        def unpack(self, raw):
            pass

    # public API rejects system-reserved MSGTYPEs
    try:
        ch.register_message_type(Sys)
        assert False, "public register_message_type should reject 0xff00"
    except channel.ChannelException as e:
        assert e.type == channel.ME_NO_MSG_TYPE
    # internal API with the system flag accepts it
    ch._register_message_type(Sys, is_system_type=True)
    assert ch._message_factories.get(0xff00) is Sys
    assert channel.SystemMessageTypes.SMT_STREAM_DATA == 0xff00
    # user types still work through the public API
    ch.register_message_type(Ping)
    assert ch._message_factories.get(Ping.MSGTYPE) is Ping
    print("ok test_system_type_registration")


def test_context_manager():
    outlet = MockOutlet(rtt=0.1)
    with channel.Channel(outlet) as ch:
        ch.register_message_type(Ping)
        ch.add_message_handler(lambda m: False)
        assert len(ch._message_callbacks) == 1
    # __exit__ -> shutdown clears handlers
    assert ch._message_callbacks == []
    print("ok test_context_manager")


if __name__ == "__main__":
    for name in list(globals()):
        if name.startswith("test_"):
            globals()[name]()
    print("all channel tests passed")
