# Route loss under an announce flood (host-side, crypto-free). Run:
#   python3 firmware/tests/test_path_in_use.py
#
# Field case (busy reference hub, ~8 announces/s): the board learned the path
# to a peer, replied, and 28 s later - when the same peer wrote again - had no
# path ("No path to c83ea345, requesting path"). ~65 other destinations had been
# learned in between; MAX_PATH_TABLE is 32 on a board without PSRAM and the
# table evicted the OLDEST entry, which was the peer we were talking to.
# Destinations in use (conversation peer, open/pending link, path waiter,
# outstanding path request) are now protected; when every entry is in use an
# unsolicited announce is not recorded at all.

import importlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
from harness import (const, Transport, MockInterface, reset_transport,
                     build_announce_hdr1, build_announce_data)
import announce_stamp_support as S
import test_lxmf_tickets as TL

transport_mod = importlib.import_module("urns.transport")
lxmf = importlib.import_module("urns.lxmf")

PEER = b"\xC8" * 16


def _d(i):
    return bytes([0x70 + (i >> 8), i & 0xFF]) + b"\x00" * 14


def _announce(dest, iface, emitted=1000):
    Transport.inbound(build_announce_hdr1(dest, data=build_announce_data(emitted=emitted)), iface)


def _setup(transport=False):
    reset_transport()
    Transport.transport_enabled = transport
    iface = MockInterface("tcp")
    Transport.interfaces = [iface]
    return iface


def test_field_case_peer_route_survives_65_announces_when_in_use():
    iface = _setup()
    assert const.MAX_PATH_TABLE == 32          # the no-PSRAM board's cap (host default)
    _announce(PEER, iface)
    assert Transport.has_path(PEER)
    Transport.mark_in_use(PEER)                # what send_message / a verified inbound message does
    for i in range(65):
        _announce(_d(i), iface, emitted=2000 + i)
    assert Transport.has_path(PEER) and PEER in Transport.path_table
    assert len(Transport.path_table) <= const.MAX_PATH_TABLE
    assert len(Transport.reachable_destinations) <= const.MAX_DESTINATIONS


def test_idle_route_is_still_evicted_oldest_first():
    iface = _setup()
    _announce(PEER, iface)
    for i in range(65):
        _announce(_d(i), iface, emitted=2000 + i)
    assert not Transport.has_path(PEER)        # unchanged policy for destinations not in use
    assert len(Transport.path_table) == const.MAX_PATH_TABLE


def test_open_link_waiter_and_outstanding_request_protect_the_route():
    iface = _setup()
    p_link, p_wait, p_req, p_idle = b"\x01" * 16, b"\x02" * 16, b"\x03" * 16, b"\x04" * 16
    for p in (p_link, p_wait, p_req, p_idle):
        _announce(p, iface)

    class D:
        hash = p_link

    class L:
        destination = D()
    Transport.active_links = [L()]
    Transport._path_waiters[p_wait] = [{"deadline": time.time() + 99, "hard": time.time() + 99}]
    Transport._path_request_times[p_req] = time.time()
    for i in range(80):
        _announce(_d(i), iface, emitted=3000 + i)
    assert Transport.has_path(p_link) and Transport.has_path(p_wait) and Transport.has_path(p_req)
    assert not Transport.has_path(p_idle)
    Transport.active_links = []
    Transport._path_waiters.clear()


def test_table_full_of_in_use_routes_does_not_record_newcomers():
    iface = _setup()
    saved = (const.MAX_PATH_TABLE, const.MAX_DESTINATIONS)
    const.MAX_PATH_TABLE = const.MAX_DESTINATIONS = 4
    try:
        keep = [_d(900 + i) for i in range(4)]
        for k in keep:
            _announce(k, iface)
            Transport.mark_in_use(k)
        for i in range(30):
            _announce(_d(i), iface, emitted=4000 + i)
        assert sorted(Transport.path_table) == sorted(keep)
        assert sorted(Transport.reachable_destinations) == sorted(keep)
        # a destination that IS in use is still recorded by displacing an idle one
        const.MAX_PATH_TABLE = const.MAX_DESTINATIONS = 5
        _announce(_d(500), iface, emitted=5000)
        assert _d(500) in Transport.path_table
        Transport.mark_in_use(_d(501))
        _announce(_d(501), iface, emitted=5001)
        assert _d(501) in Transport.path_table and _d(500) not in Transport.path_table
        assert len(Transport.path_table) <= 5
    finally:
        const.MAX_PATH_TABLE, const.MAX_DESTINATIONS = saved


def test_in_use_set_is_bounded_and_expires():
    reset_transport()
    real = transport_mod.time

    class T:
        t = 10000.0
        def time(self):
            return T.t
        def __getattr__(self, n):
            return getattr(real, n)
    transport_mod.time = T()
    try:
        for i in range(60):
            Transport.mark_in_use(_d(i))
            T.t += 1
        assert len(Transport._in_use) <= transport_mod._IN_USE_MAX
        assert Transport.is_in_use(_d(59)) and not Transport.is_in_use(_d(0))
        T.t += transport_mod._IN_USE_TTL + 1
        assert not Transport.is_in_use(_d(59))
    finally:
        transport_mod.time = real


def test_relay_keeps_in_use_route_and_stays_bounded():
    iface = _setup(transport=True)
    _announce(PEER, iface)
    Transport.mark_in_use(PEER)
    for i in range(100):
        _announce(_d(i), iface, emitted=6000 + i)
    assert PEER in Transport.path_table
    assert len(Transport.path_table) <= const.MAX_PATH_TABLE
    assert len(Transport.announce_table) <= const.MAX_ANNOUNCE_TABLE
    # forwarding to the protected peer still resolves its route
    assert Transport.path_table[PEER][const.IDX_PT_NEXT_HOP] == PEER


def test_known_destinations_keep_in_use_peer_key():
    RI = S.RealIdentity
    reset_transport()
    saved = const.MAX_KNOWN_DESTINATIONS
    const.MAX_KNOWN_DESTINATIONS = 5
    RI.known_destinations.clear()
    try:
        pub = b"\x11" * 64
        RI.remember(b"h", PEER, pub)
        Transport.mark_in_use(PEER)
        for i in range(40):
            time.sleep(0.0005)
            RI.remember(b"h", _d(i), pub)
        assert PEER in RI.known_destinations and len(RI.known_destinations) <= 5
        for i in range(5):                                  # everything in use: newcomer dropped
            Transport.mark_in_use(_d(100 + i))
        RI.known_destinations.clear()
        for i in range(5):
            RI.remember(b"h", _d(100 + i), pub)
        RI.remember(b"h", _d(300), pub)
        assert _d(300) not in RI.known_destinations and len(RI.known_destinations) == 5
    finally:
        const.MAX_KNOWN_DESTINATIONS = saved
        RI.known_destinations.clear()


def test_lxmf_send_and_verified_receive_mark_the_peer_in_use():
    r, peer = TL._router_with_peer()
    old_link, old_dest = TL.link_mod.OutgoingLink, lxmf.Destination
    TL.link_mod.OutgoingLink = TL.StubOutgoingLink
    lxmf.Destination = TL.FakeDest
    TL.StubOutgoingLink.instances = []
    Transport._in_use.clear()
    try:
        assert not Transport.is_in_use(peer.hash)
        r.send_message(peer.hash, "x", desired_method=lxmf.LXMessage.DIRECT)
        assert Transport.is_in_use(peer.hash)
        # inbound messages: only a VALIDATED signature marks the sender
        Transport._in_use.clear()
        sender = TL.FakeIdentity(b"sender")
        lxmf.Identity.known[sender.hash] = sender
        dst, _ = TL._pair()
        msg = lxmf.LXMessage(destination=dst, source=TL.FakeDest(sender), content=b"hi", title=b"")
        msg.pack()
        old_vs = lxmf.LXMRouter.verify_signatures
        lxmf.LXMRouter.verify_signatures = True
        TL.FakeIdentity.validate = lambda self, sig, data: False
        back = lxmf.LXMessage.unpack_from_bytes(msg.packed)
        assert not back.signature_validated and not Transport.is_in_use(sender.hash)
        TL.FakeIdentity.validate = lambda self, sig, data: True
        back = lxmf.LXMessage.unpack_from_bytes(msg.packed)
        assert back.signature_validated and Transport.is_in_use(sender.hash)
        lxmf.LXMRouter.verify_signatures = old_vs
    finally:
        TL.link_mod.OutgoingLink, lxmf.Destination = old_link, old_dest
        lxmf.Identity.known.clear()
        Transport.reachable_destinations.clear()
        Transport._path_waiters.clear()
        Transport._in_use.clear()
        if hasattr(TL.FakeIdentity, "validate"):
            del TL.FakeIdentity.validate


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for k, v in tests:
        try:
            v()
            print("PASS", k)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print("FAIL", k, repr(e))
            failed += 1
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
