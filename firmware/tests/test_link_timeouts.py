# RTT/bitrate-aware link timeout tests (host-side, crypto-free).
#
# Covers: OutgoingLink establishment timeout scaling with the first-hop
# interface bitrate (per_hop = DEFAULT_PER_HOP_TIMEOUT + 3 full-MTU airtimes,
# capped at 20 s, times hops+1, plus the crypto grace), and the request
# timeout deriving from the measured link RTT instead of fixed per-hop
# constants.
#
# Run:  python3 firmware/tests/test_link_timeouts.py

import sys
import os
import time as _pytime
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
from harness import const, Transport, MockInterface, reset_transport, set_identity, link

import time
if not hasattr(time, "ticks_ms"):
    time.ticks_ms = lambda: int(_pytime.monotonic() * 1000)
    time.ticks_diff = lambda a, b: a - b

DEST = b"\xD2" * 16


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


_fc = types.ModuleType("urns.crypto")
_fc.X25519PrivateKey = _FakeXPriv
sys.modules["urns.crypto"] = _fc
# With the fake crypto package in place the native-flag probe fails closed
# to the pure-Python grace — deterministic for these tests.
link._crypto_grace = None
GRACE = link._crypto_grace_s()


class _Dest:
    def __init__(self, h=DEST):
        self.hash = h
        self.hexhash = h.hex()
        self.type = const.DEST_SINGLE


def _setup(iface, hops=1, seed_path=True):
    reset_transport()
    set_identity()
    Transport.interfaces = [iface]
    if seed_path:
        Transport.path_table[DEST] = [time.time(), DEST, hops,
                                      time.time() + 3600, iface, None, 0]


def _expected(bitrate, hops):
    hop_air = (const.MTU * 8) / bitrate if bitrate > 0 else 0
    per_hop = min(const.DEFAULT_PER_HOP_TIMEOUT + 3 * hop_air, 20)
    return per_hop * (max(1, hops) + 1) + GRACE


def test_crypto_grace_fails_closed():
    assert GRACE == 12, "fake crypto package -> pure-Python grace"


def test_establishment_tcp_one_hop():
    iface = MockInterface("tcp", hw_mtu=16384, bitrate=10_000_000)
    _setup(iface, hops=1)
    ol = link.OutgoingLink(_Dest())
    assert abs(ol.establishment_timeout - _expected(10_000_000, 1)) < 1e-6
    # ~12 s of timeout + grace — was 55 s flat before.
    assert ol.establishment_timeout < 25


def test_establishment_sf11_two_hops_caps_per_hop():
    iface = MockInterface("lora", hw_mtu=508, bitrate=537)
    _setup(iface, hops=2)
    ol = link.OutgoingLink(_Dest())
    # hop_air ≈ 7.45 s -> per_hop caps at 20 -> 20*3 + grace
    assert abs(ol.establishment_timeout - (20 * 3 + GRACE)) < 1e-6


def test_establishment_unknown_bitrate_uses_flat_per_hop():
    iface = MockInterface("mock", hw_mtu=500, bitrate=0)
    _setup(iface, hops=3)
    ol = link.OutgoingLink(_Dest())
    assert abs(ol.establishment_timeout
               - (const.DEFAULT_PER_HOP_TIMEOUT * 4 + GRACE)) < 1e-6


def test_establishment_no_path_conservative():
    iface = MockInterface("tcp", hw_mtu=16384, bitrate=10_000_000)
    _setup(iface, seed_path=False)
    ol = link.OutgoingLink(_Dest())
    # No path: hops 0 -> treated as 1, no bitrate -> flat per-hop.
    assert abs(ol.establishment_timeout
               - (const.DEFAULT_PER_HOP_TIMEOUT * 2 + GRACE)) < 1e-6


def test_request_timeout_scales_with_rtt():
    OL = link.OutgoingLink
    ol = object.__new__(OL)
    ol.status = OL.ACTIVE
    ol.link_id = b"\x40" * 16
    ol.mtu = 500
    ol.sdu = 465
    ol.mdu = 431
    ol.pending_requests = {}
    ol.rtt = 0.05
    reset_transport()
    Transport.interfaces = [MockInterface("tcp")]

    class _PassToken:
        def encrypt(self, d):
            return bytes(d)

    ol._token = _PassToken()
    rid = ol.request("/page/index.mu")
    assert abs(ol.pending_requests[rid][2] - (0.05 * 6 + 11.25)) < 1e-9
    ol.rtt = 4.0
    rid2 = ol.request("/page/two.mu")
    assert abs(ol.pending_requests[rid2][2] - (4.0 * 6 + 11.25)) < 1e-9


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("ok " + t.__name__)
        passed += 1
    print("\n%d/%d passed" % (passed, len(tests)))
