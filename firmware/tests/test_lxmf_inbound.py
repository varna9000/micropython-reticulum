# LXMF inbound paths (opportunistic packet, link packet, resource): delivery,
# duplicate suppression, transport_encrypted flag, link identity bookkeeping,
# rejection of too-short / response resources, and ticket capture on each path.
#
# Run:  python3 firmware/tests/test_lxmf_inbound.py

import sys
import os
import time
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
import importlib

lxmf = importlib.import_module("urns.lxmf")
umsgpack = importlib.import_module("urns.umsgpack")
resource_mod = importlib.import_module("urns.resource")
const_mod = importlib.import_module("urns.const")
LXMessage = lxmf.LXMessage
LXMRouter = lxmf.LXMRouter

DEST = b"\x01" * 16
SRC = b"\x02" * 16


class _Dest:
    hash = DEST


def _raw(content, ts=1.0, fields=None):
    payload = umsgpack.packb([ts, b"t", content, fields or {}])
    return DEST + SRC + b"\x00" * 64 + payload


def _router():
    r = LXMRouter(storagepath=None)
    r.delivery_destination = _Dest()
    got = []
    r.register_delivery_callback(got.append)
    return r, got


class _Link:
    pass


class _Res:
    def __init__(self, data, request_id=None, status=None):
        self.status = resource_mod.COMPLETE if status is None else status
        self.request_id = request_id
        self.data = data
        self.link = _Link()


def test_link_packet_delivers_once_and_marks_transport_encrypted():
    r, got = _router()
    raw = _raw(b"hello")
    r._link_packet_received(raw, None)
    r._delivery_packet(raw[16:], None)            # same message again: duplicate
    assert len(got) == 1 and got[0].transport_encrypted


def test_opportunistic_is_not_transport_encrypted():
    r, got = _router()
    r._delivery_packet(_raw(b"hello2", 2.0)[16:], None)
    assert len(got) == 1 and not got[0].transport_encrypted


def test_resource_delivers_and_records_link_source():
    r, got = _router()
    res = _Res(_raw(b"hello3", 3.0))
    r._handle_resource_concluded(res)
    assert len(got) == 1 and got[0].transport_encrypted
    assert res.link.lxmf_source_hash == SRC


def test_resource_response_and_incomplete_are_ignored():
    r, got = _router()
    r._handle_resource_concluded(_Res(_raw(b"x", 4.0) + b"z", request_id=b"x"))
    r._handle_resource_concluded(_Res(_raw(b"y", 5.0), status=resource_mod.COMPLETE + 1))
    assert got == []


def test_resource_too_short_is_ignored():
    r, got = _router()
    r._handle_resource_concluded(_Res(b"\x00" * (2 * 16 + 64 - 1)))
    r._handle_resource_concluded(_Res(None))
    r._handle_resource_concluded(_Res(b""))
    assert got == []


def test_garbage_payload_does_not_raise_or_deliver():
    r, got = _router()
    r._link_packet_received(DEST + SRC + b"\x00" * 64 + b"\xc1\xc1", None)
    r._delivery_packet(b"\x01\x02\x03", None)
    r._handle_resource_concluded(_Res(DEST + SRC + b"\x00" * 64 + b"\xc1"))
    assert got == []


# ---- ticket capture on every inbound path ----

class _Id:
    def __init__(self, seed):
        self.hash = hashlib.sha256(seed).digest()[:16]
        self._seed = seed

    def sign(self, data):
        return hashlib.sha512(self._seed + data).digest()

    def get_public_key(self):
        return hashlib.sha512(self._seed).digest()


class _FDest:
    OUT = 0x12
    SINGLE = 0x00

    def __init__(self, identity, *args):
        self.identity = identity
        self.hash = identity.hash

    def sign(self, data):
        return self.identity.sign(data)


def _ticket_case(deliver):
    r = LXMRouter(storagepath=None)
    me, peer = _Id(b"me"), _Id(b"peer")
    r.delivery_destination = _FDest(me)
    lxmf.Identity.known[peer.hash] = peer
    lxmf.Identity.known[me.hash] = me
    got = []
    r.register_delivery_callback(got.append)
    old_v, old_d = LXMRouter.verify_signatures, lxmf.Destination
    LXMRouter.verify_signatures = False
    lxmf.Destination = _FDest
    try:
        m = LXMessage(destination=r.delivery_destination, source=_FDest(peer),
                      content=b"x",
                      fields={lxmf.FIELD_TICKET: [time.time() + 3600, b"\x07" * 16]})
        m.pack()
        deliver(r, m.packed)
        assert r.get_outbound_ticket(peer.hash) == b"\x07" * 16
        assert len(got) == 1
    finally:
        LXMRouter.verify_signatures, lxmf.Destination = old_v, old_d
        lxmf.Identity.known.clear()


def test_ticket_captured_from_link_packet():
    _ticket_case(lambda r, p: r._link_packet_received(p, None))


def test_ticket_captured_from_opportunistic_packet():
    _ticket_case(lambda r, p: r._delivery_packet(p[16:], None))


def test_ticket_captured_from_resource():
    _ticket_case(lambda r, p: r._handle_resource_concluded(_Res(p)))


# ---- peers table bound ----

def test_peers_are_capped_and_never_evict_a_destination_with_queued_messages():
    import random
    r = LXMRouter(storagepath=None)
    rnd = random.Random(7)
    t = [1000]
    real_time = time.time
    time.time = lambda: t[0]
    try:
        # a destination we have a message queued for, learned first (so it is
        # the OLDEST peer and the first eviction candidate)
        pending = b"\x99" * 16
        r.handle_announce(pending, None)
        m = LXMessage(destination_hash=pending, source_hash=b"\x01" * 16, content=b"x")
        r.pending_outbound.append(m)
        batch = LXMRouter._peer_evict_batch()
        scans = [0]
        real_evict = r._evict_peers

        def counting():
            scans[0] += 1
            return real_evict()

        r._evict_peers = counting
        for _ in range(5000):
            t[0] += 1
            r.handle_announce(bytes(rnd.getrandbits(8) for _ in range(16)), None)
            assert len(r.peers) <= const_mod.MAX_DESTINATIONS
        assert pending in r.peers, "evicted a destination with a queued message"
        assert const_mod.MAX_DESTINATIONS - batch < len(r.peers) <= const_mod.MAX_DESTINATIONS
        # Batched: one table scan per `batch` new peers, not one per announce. Found on a
        # T-Deck Pro on a busy hub: a per-announce min() over 512 peers grew
        # handle_announce from ~5 ms to ~45 ms once the table filled.
        new_after_full = 5001 - const_mod.MAX_DESTINATIONS
        assert batch > 1 and scans[0] <= new_after_full // batch + 1, (scans[0], batch)
        # the least recently heard idle peer goes first (fill to the cap, then one more)
        while len(r.peers) < const_mod.MAX_DESTINATIONS:
            t[0] += 1
            r.handle_announce(bytes(rnd.getrandbits(8) for _ in range(16)), None)
        oldest = min((k for k in r.peers if k != pending), key=lambda k: r.peers[k]["timestamp"])
        newest = max(r.peers, key=lambda k: r.peers[k]["timestamp"])
        t[0] += 1
        r.handle_announce(b"\x77" * 16, None)
        assert oldest not in r.peers and b"\x77" * 16 in r.peers
        assert newest in r.peers and pending in r.peers
        # a re-announce of a known peer never evicts
        n = len(r.peers)
        r.handle_announce(b"\x77" * 16, None)
        assert len(r.peers) == n
    finally:
        time.time = real_time


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok " + t.__name__)
    print("\n%d passed" % len(tests))
