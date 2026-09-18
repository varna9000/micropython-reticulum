# LXMF ticket stamps + DIRECT-link identification (host-side).
#
# Reference LXMF (>=0.5, MeshChat/Sideband/NomadNet) can require a stamp on
# every inbound message. Proof-of-work stamps are out of reach for an MCU, but
# a peer that requires stamps hands us a TICKET (FIELD_TICKET = [expires,
# 16-byte ticket]) in the messages it sends us, and a stamp derived from that
# ticket -- truncated_hash(ticket + message_id) -- validates for free. The
# stamp rides as the 5th payload element, appended AFTER the hash/signature
# are computed over the 4-element payload (reference LXMessage.pack()).
#
# Reference LXMF also identifies itself on every delivery link it opens
# ("backchannel identification"). MeshChatX's inbound policy uses that
# identity to tell contacts from strangers BEFORE accepting a Resource; a link
# we leave anonymous is a "stranger" even when the node is in the contact
# list, and the reply is cancelled before transfer. So a fresh DIRECT link
# must identify with the delivery identity before the payload goes out.
#
# Run:  python3 firmware/tests/test_lxmf_tickets.py

import sys
import os
import time
import hashlib
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
import importlib

lxmf = importlib.import_module("urns.lxmf")
umsgpack = importlib.import_module("urns.umsgpack")
link_mod = importlib.import_module("urns.link")
transport = importlib.import_module("urns.transport")
Transport = transport.Transport
LXMessage = lxmf.LXMessage
LXMRouter = lxmf.LXMRouter


class FakeIdentity:
    """Crypto-free identity: deterministic 64B 'signature', 64B pubkey."""
    def __init__(self, seed):
        self.hash = hashlib.sha256(seed).digest()[:16]
        self.hexhash = self.hash.hex()
        self._seed = seed

    def sign(self, data):
        return hashlib.sha512(self._seed + data).digest()

    def get_public_key(self):
        return hashlib.sha512(self._seed).digest()


class FakeDest:
    OUT = 0x12
    SINGLE = 0x00

    def __init__(self, identity, *args):
        self.identity = identity
        self.hash = identity.hash
        self.hexhash = identity.hash.hex()

    def sign(self, data):
        return self.identity.sign(data)


def _pair():
    return FakeDest(FakeIdentity(b"dst")), FakeDest(FakeIdentity(b"src"))


def _truncated(data):
    return hashlib.sha256(data).digest()[:16]


# ---------------------------------------------------------------- pack ----

def test_pack_without_ticket_keeps_four_element_payload():
    dst, src = _pair()
    m = LXMessage(destination=dst, source=src, content=b"hi", title=b"")
    m.pack()
    payload = umsgpack.unpackb(m.packed[32 + 64:])
    assert len(payload) == 4
    assert m.stamp is None


def test_pack_appends_ticket_stamp_after_hash_and_signature():
    dst, src = _pair()
    ticket = bytes(range(16))
    plain = LXMessage(destination=dst, source=src, content=b"hi", title=b"")
    plain.timestamp = 1700000000
    plain.pack()
    stamped = LXMessage(destination=dst, source=src, content=b"hi", title=b"")
    stamped.timestamp = 1700000000
    stamped.outbound_ticket = ticket
    stamped.pack()
    # hash/message_id/signature are over the 4-element payload -> unchanged
    assert stamped.hash == plain.hash
    assert stamped.message_id == plain.message_id
    assert stamped.signature == plain.signature
    payload = umsgpack.unpackb(stamped.packed[32 + 64:])
    assert len(payload) == 5
    assert payload[4] == _truncated(ticket + stamped.message_id)
    assert stamped.stamp == payload[4]


def test_unpack_of_stamped_message_recovers_same_hash():
    dst, src = _pair()
    m = LXMessage(destination=dst, source=src, content=b"round trip", title=b"t")
    m.outbound_ticket = b"\x01" * 16
    m.pack()
    back = LXMessage.unpack_from_bytes(m.packed)
    assert back.hash == m.hash
    assert back.content == b"round trip"
    assert back.title == b"t"


# ------------------------------------------------------------- tickets ----

def _inbound(source_hash, fields, validated=True):
    m = LXMessage(destination_hash=b"\xdd" * 16, source_hash=source_hash,
                  fields=fields)
    m.signature_validated = validated
    return m


def test_router_remembers_valid_inbound_ticket():
    r = LXMRouter()
    src = b"\xaa" * 16
    ticket = b"\x42" * 16
    expires = time.time() + 3600
    r.remember_ticket(_inbound(src, {lxmf.FIELD_TICKET: [expires, ticket]}))
    assert r.get_outbound_ticket(src) == ticket
    assert r.get_outbound_ticket(b"\xbb" * 16) is None


def test_router_ignores_expired_malformed_or_unsigned_tickets():
    r = LXMRouter()
    src = b"\xaa" * 16
    r.remember_ticket(_inbound(src, {lxmf.FIELD_TICKET: [time.time() - 1, b"\x42" * 16]}))
    assert r.get_outbound_ticket(src) is None
    r.remember_ticket(_inbound(src, {lxmf.FIELD_TICKET: [time.time() + 60, b"\x42" * 15]}))
    assert r.get_outbound_ticket(src) is None
    r.remember_ticket(_inbound(src, {lxmf.FIELD_TICKET: b"\x42" * 16}))
    assert r.get_outbound_ticket(src) is None
    r.remember_ticket(_inbound(src, {lxmf.FIELD_TICKET: [time.time() + 60, b"\x42" * 16]},
                               validated=False))
    assert r.get_outbound_ticket(src) is None
    r.remember_ticket(_inbound(src, {}))
    assert r.get_outbound_ticket(src) is None


def test_expired_remembered_ticket_is_not_returned():
    r = LXMRouter()
    src = b"\xaa" * 16
    r.remember_ticket(_inbound(src, {lxmf.FIELD_TICKET: [time.time() + 1, b"\x42" * 16]}))
    r.outbound_tickets[src][0] = time.time() - 1      # let it lapse
    assert r.get_outbound_ticket(src) is None


def test_tickets_persist_across_router_instances():
    d = tempfile.mkdtemp()
    src = b"\xaa" * 16
    ticket = b"\x42" * 16
    r1 = LXMRouter(storagepath=d)
    r1.remember_ticket(_inbound(src, {lxmf.FIELD_TICKET: [time.time() + 3600, ticket]}))
    r2 = LXMRouter(storagepath=d)
    assert r2.get_outbound_ticket(src) == ticket


def test_router_defaults_storagepath_to_node_storage():
    """No app passes storagepath; Reticulum publishes its storage dir via
    Identity.storagepath, so the router must pick that up or tickets would
    never survive a reboot."""
    d = tempfile.mkdtemp()
    old = getattr(lxmf.Identity, "storagepath", None)
    lxmf.Identity.storagepath = d
    try:
        assert LXMRouter().storagepath == d
        assert LXMRouter(storagepath="/x").storagepath == "/x"
    finally:
        lxmf.Identity.storagepath = old


def test_inbound_handlers_remember_ticket():
    """Every inbound path (opportunistic packet, link packet, resource) must
    capture the ticket -- that's where the peer hands it to us."""
    r = LXMRouter()
    src_id = FakeIdentity(b"peer")
    r.delivery_destination = FakeDest(FakeIdentity(b"me"))
    lxmf.Identity.known[src_id.hash] = src_id
    lxmf.Identity.known[r.delivery_destination.hash] = r.delivery_destination.identity
    old_verify, old_dest = LXMRouter.verify_signatures, lxmf.Destination
    LXMRouter.verify_signatures = False        # fake identity can't validate
    lxmf.Destination = FakeDest
    try:
        m = LXMessage(destination=r.delivery_destination, source=FakeDest(src_id),
                      content=b"x",
                      fields={lxmf.FIELD_TICKET: [time.time() + 3600, b"\x07" * 16]})
        m.pack()
        r._link_packet_received(m.packed, None)
        assert r.get_outbound_ticket(src_id.hash) == b"\x07" * 16
    finally:
        LXMRouter.verify_signatures, lxmf.Destination = old_verify, old_dest
        lxmf.Identity.known.clear()


# ------------------------------------------------ DIRECT link identify ----

class StubOutgoingLink:
    """Records the order of identify()/send() calls; establishes on demand."""
    instances = []

    def __init__(self, destination, established_callback=None, closed_callback=None, **kw):
        self.destination = destination
        self.status = 0x01
        self.link_id = b"\x11" * 16
        self.calls = []
        self.resource_concluded_callback = None
        self._established = established_callback
        self._closed = closed_callback
        StubOutgoingLink.instances.append(self)

    def fail_establishment(self):
        self.status = 0x02
        self._closed(self)

    def identify(self, identity):
        self.calls.append(("identify", identity))

    def send(self, data, context=0):
        self.calls.append(("send", data))

    def teardown(self):
        self.calls.append(("teardown", None))

    def establish(self):
        self._established(self)


def _router_with_peer():
    r = LXMRouter()
    me = FakeIdentity(b"me")
    peer = FakeIdentity(b"peer")
    r.delivery_identity = me
    r.delivery_destination = FakeDest(me)
    lxmf.Identity.known[peer.hash] = peer
    Transport.reachable_destinations[peer.hash] = time.time()
    Transport.active_links = []
    return r, peer


def test_direct_delivery_identifies_before_payload_on_fresh_link():
    r, peer = _router_with_peer()
    old_link, old_dest = link_mod.OutgoingLink, lxmf.Destination
    link_mod.OutgoingLink = StubOutgoingLink
    lxmf.Destination = FakeDest
    StubOutgoingLink.instances = []
    try:
        msg = r.send_message(peer.hash, "short reply", desired_method=LXMessage.DIRECT)
        assert msg is not None and msg.method == LXMessage.DIRECT
        link = StubOutgoingLink.instances[0]
        link.establish()
        kinds = [c[0] for c in link.calls]
        assert kinds[:2] == ["identify", "send"], kinds
        assert link.calls[0][1] is r.delivery_identity
    finally:
        link_mod.OutgoingLink, lxmf.Destination = old_link, old_dest
        lxmf.Identity.known.clear()
        Transport.reachable_destinations.clear()


def test_send_message_applies_remembered_ticket():
    r, peer = _router_with_peer()
    ticket = b"\x99" * 16
    r.remember_ticket(_inbound(peer.hash, {lxmf.FIELD_TICKET: [time.time() + 3600, ticket]}))
    old_link, old_dest = link_mod.OutgoingLink, lxmf.Destination
    link_mod.OutgoingLink = StubOutgoingLink
    lxmf.Destination = FakeDest
    try:
        msg = r.send_message(peer.hash, "stamped", desired_method=LXMessage.DIRECT)
        payload = umsgpack.unpackb(msg.packed[32 + 64:])
        assert len(payload) == 5
        assert payload[4] == _truncated(ticket + msg.message_id)
    finally:
        link_mod.OutgoingLink, lxmf.Destination = old_link, old_dest
        lxmf.Identity.known.clear()
        Transport.reachable_destinations.clear()


def test_direct_retry_waits_for_path_when_route_expired():
    """A link establishment timeout expires the path (reference RNS). The
    LXMF retry must then wait for a fresh route via ensure_path instead of
    opening another link on a route that no longer exists (seen live: three
    consecutive 30 s timeouts after a transport node's uplink changed)."""
    r, peer = _router_with_peer()
    old_link, old_dest = link_mod.OutgoingLink, lxmf.Destination
    link_mod.OutgoingLink = StubOutgoingLink
    lxmf.Destination = FakeDest
    StubOutgoingLink.instances = []
    Transport._path_waiters.clear()
    try:
        r.send_message(peer.hash, "reply", desired_method=LXMessage.DIRECT)
        first = StubOutgoingLink.instances[0]
        # Establishment times out: the link layer has expired the path.
        Transport.expire_path(peer.hash)
        first.fail_establishment()
        assert len(StubOutgoingLink.instances) == 1, "must not re-link on a dead route"
        assert peer.hash in Transport._path_waiters
        # Route rediscovered (path response announce) -> the retry proceeds.
        Transport.reachable_destinations[peer.hash] = time.time()
        Transport._process_path_waiters()
        assert len(StubOutgoingLink.instances) == 2
    finally:
        link_mod.OutgoingLink, lxmf.Destination = old_link, old_dest
        lxmf.Identity.known.clear()
        Transport.reachable_destinations.clear()
        Transport._path_waiters.clear()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("ok " + t.__name__)
        passed += 1
    print("\n%d passed" % passed)
