# Initiator-side link MTU signalling tests (host-side, crypto-free).
#
# Covers: Transport.next_hop_interface_hw_mtu, OutgoingLink proposing the
# next-hop HW_MTU in its link-request signalling bytes (with the no-path
# fallback to protocol MTU), the per-packet MTU ceiling on the initiator
# send paths (OutgoingLink.send / OutgoingLink.request), responder Link
# growing an .mdu, and Transport.outbound skipping interfaces whose HW_MTU
# a big link packet exceeds.
#
# Run:  python3 firmware/tests/test_link_mtu.py

import sys
import os
import time as _pytime
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
from harness import (const, Transport, MockInterface, reset_transport,
                     set_identity, link, parse_signalling)

# MicroPython time API used by Link.__init__'s ECDH timing — shim for CPython.
import time
if not hasattr(time, "ticks_ms"):
    time.ticks_ms = lambda: int(_pytime.monotonic() * 1000)
    time.ticks_diff = lambda a, b: a - b

DEST = b"\xD0" * 16


# --- fake crypto (injected before any lazy `from .crypto import ...`) ------
class _FakeXPriv:
    @staticmethod
    def generate():
        return _FakeXPriv()

    def public_key(self):
        return self

    def public_bytes(self):
        return b"\x22" * 32

    def exchange(self, peer):
        return b"\x33" * 32


class _FakeXPub:
    @staticmethod
    def from_public_bytes(b):
        return _FakeXPub()


class _FakeToken:
    def __init__(self, key=b""):
        pass

    def encrypt(self, data):
        return bytes(data)

    def decrypt(self, data):
        return bytes(data)


def _fake_hkdf(length=64, derive_from=b"", salt=b"", context=b""):
    return b"\x44" * length


_fc = types.ModuleType("urns.crypto")
_fc.X25519PrivateKey = _FakeXPriv
_fc.X25519PublicKey = _FakeXPub
_fc.Token = _FakeToken
_fc.hkdf = _fake_hkdf
sys.modules["urns.crypto"] = _fc


class _FakeSigIdentity:
    sig_pub_bytes = b"\x55" * 32

    def sign(self, data):
        return b"\x66" * 64


class _Dest:
    def __init__(self, h=DEST):
        self.hash = h
        self.hexhash = h.hex()
        self.type = const.DEST_SINGLE
        self.identity = _FakeSigIdentity()


def _seed_path(iface, hops=1):
    # [ts, next_hop, hops, expires, recv_if, announce, emitted]
    Transport.path_table[DEST] = [time.time(), DEST, hops,
                                  time.time() + 3600, iface, None, 0]


def _setup(iface):
    reset_transport()
    set_identity()
    link.Link._last_creation = 0
    Transport.interfaces = [iface]


def test_next_hop_interface_hw_mtu():
    iface = MockInterface("tcp", hw_mtu=16384)
    _setup(iface)
    assert Transport.next_hop_interface_hw_mtu(DEST) is None, "no path -> None"
    _seed_path(iface)
    assert Transport.next_hop_interface_hw_mtu(DEST) == 16384
    Transport.path_table[DEST][const.IDX_PT_RECV_IF] = None
    assert Transport.next_hop_interface_hw_mtu(DEST) is None


def test_outlink_signals_next_hop_mtu():
    iface = MockInterface("tcp", hw_mtu=16384)
    _setup(iface)
    _seed_path(iface)
    ol = link.OutgoingLink(_Dest())
    assert ol.mtu == 16384, ol.mtu
    assert ol.mdu == link._link_mdu(16384)
    assert len(iface.sent) == 1, "link request must go out"
    mtu, mode = parse_signalling(iface.sent[0][-3:])
    assert mtu == 16384, mtu


def test_outlink_no_path_falls_back_to_protocol_mtu():
    iface = MockInterface("tcp", hw_mtu=16384)
    _setup(iface)
    ol = link.OutgoingLink(_Dest())
    assert ol.mtu == const.MTU
    mtu, mode = parse_signalling(iface.sent[0][-3:])
    assert mtu == const.MTU


def test_outlink_send_carries_big_packets():
    iface = MockInterface("tcp", hw_mtu=16384)
    _setup(iface)
    OL = link.OutgoingLink
    ol = object.__new__(OL)
    ol.status = OL.ACTIVE
    ol.link_id = b"\x10" * 16
    ol._token = _FakeToken()
    ol.mtu = 8192
    ol.last_outbound = 0
    ol.send(b"x" * 2000)   # would raise OSError at pack() without packet.MTU
    assert len(iface.sent) == 1 and len(iface.sent[0]) > 2000


def test_outlink_request_gate_and_mtu():
    iface = MockInterface("tcp", hw_mtu=16384)
    _setup(iface)
    OL = link.OutgoingLink
    ol = object.__new__(OL)
    ol.status = OL.ACTIVE
    ol.link_id = b"\x10" * 16
    ol._token = _FakeToken()
    ol.mtu = 8192
    ol.sdu = 8192 - const.HEADER_MAXSIZE - const.IFAC_MIN_SIZE
    ol.mdu = link._link_mdu(8192)
    ol.pending_requests = {}
    ol.rtt = 0
    ol.destination = _Dest()
    _seed_path(iface)
    rid = ol.request("/big", data=b"y" * 2000)
    assert rid is not None, "2KB request must pass on an 8K link"
    assert len(iface.sent) == 1
    assert ol.request("/too-big", data=b"y" * 9000) is None, "mdu gate holds"


def test_responder_link_negotiates_and_has_mdu():
    iface = MockInterface("tcp", hw_mtu=16384)
    _setup(iface)

    class _LRPacket:
        data = b"\x11" * 64 + link._signalling_bytes(16384, 0x01)
        receiving_interface = iface
        hops = 1
        raw = b"\x00\x01" + b"\x00" * 66

        def get_hashable_part(self):
            return b"\x00" * 20 + self.data

    lk = link.Link(_Dest(), _LRPacket())
    assert lk.mtu == 16384
    assert lk.sdu == 16384 - const.HEADER_MAXSIZE - const.IFAC_MIN_SIZE
    assert lk.mdu == link._link_mdu(16384), "responder Link must carry .mdu"


def test_outbound_skips_undersized_interfaces():
    lora = MockInterface("lora", hw_mtu=508)
    tcp = MockInterface("tcp", hw_mtu=16384)
    _setup(lora)
    Transport.interfaces = [lora, tcp]
    OL = link.OutgoingLink
    ol = object.__new__(OL)
    ol.status = OL.ACTIVE
    ol.link_id = b"\x10" * 16
    ol._token = _FakeToken()
    ol.mtu = 8192
    ol.last_outbound = 0
    ol.send(b"x" * 2000)
    assert len(tcp.sent) == 1, "big-MTU interface must carry the packet"
    assert len(lora.sent) == 0, "undersized interface must be skipped quietly"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("ok " + t.__name__)
        passed += 1
    print("\n%d/%d passed" % (passed, len(tests)))
