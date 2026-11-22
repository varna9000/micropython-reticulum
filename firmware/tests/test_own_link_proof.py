# LXMF over a link WE opened, both ends REAL (review finding 2). Run:
#   python3 firmware/tests/test_own_link_proof.py
#
# Node A = the port's LXMRouter + a REAL OutgoingLink (created by LXMRouter._new_link).
# Node B = a peer built from the port's REAL inbound Link (Destination link-request path).
# Frames are moved by hand (ownlink_harness). B plays the reference peer's backchannel:
# it sends an LXMF packet / Resource to A over the very link A opened. What the reference
# peer does with the answer (RNS 1.6.1 Link.prove_packet / PacketReceipt.validate_link_proof):
# the proof is explicit (packet_hash + signature), signed with the proving side's LINK key and
# validated against peer_sig_pub = bytes 32..64 of the link request. B's receipt therefore
# validates against the sig-pub it read off the wire (SigPubVerifier below), exactly as the
# reference does -- NOT against any identity.

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ownlink_harness as H
from ownlink_harness import (const, Node, Transport, Destination, Identity, Packet, packet_mod,
                             link_mod, lxmf, resource_mod, parse)
from urns.crypto import Ed25519PublicKey

LXMessage, LXMRouter = lxmf.LXMessage, lxmf.LXMRouter


class SigPubVerifier:
    """What the reference peer's Link.validate does: verify with the sig-pub from the request."""

    def __init__(self, sig_pub):
        self.pub = Ed25519PublicKey.from_public_bytes(sig_pub)

    def validate(self, signature, message):
        try:
            self.pub.verify(signature, message)
            return True
        except Exception:
            return False


class World:
    def __init__(self):
        # links we open sign proofs only on a board with native Ed25519 (else a blocking pure-Python
        # key generation per link); this test is about the signed case, so pretend this host has it.
        lxmf.ed25519.have_native = lambda: True
        link_mod.Link._last_creation = 0        # the responder's per-process link-creation cooldown
        self.a, self.b = Node("A"), Node("B")
        self.got = []
        self.delivered = []
        with self.b.ctx():
            self.b_dest = Destination(self.b.identity, Destination.IN, Destination.SINGLE,
                                      "lxmf", "delivery")
            self.b_links = []
            self.b_dest.set_link_established_callback(lambda l: self.b_links.append(l))
        with self.a.ctx():
            self.router = LXMRouter(identity=self.a.identity)
            self.router.register_delivery_identity(self.a.identity, display_name="A")
            self.router.register_delivery_callback(lambda m: self.got.append(m))
            self.router.register_outbound_callbacks(delivered=lambda m: self.delivered.append(m))
        with self.b.ctx():
            self.b_dest.announce()
        self.a.take()
        for f in self.b.take():
            self.a.deliver(f)
        assert Identity.recall(self.b_dest.hash) is not None, "A did not learn B's identity"

    def pump(self, hold=None):
        """Move frames until quiet. hold(frame) -> True keeps a frame back (returned)."""
        held = []
        for _ in range(60):
            moved = False
            for src, dst in ((self.a, self.b), (self.b, self.a)):
                for f in src.take():
                    if hold is not None and hold(src, f):
                        held.append((src, dst, f))
                        continue
                    moved = True
                    dst.deliver(f)
            if not moved:
                break
        return held

    def release(self, held):
        for src, dst, f in held:
            dst.deliver(f)


def _b_message(w, content):
    """A genuine, signed LXMF message B -> A (packed bytes)."""
    with w.b.ctx():
        ident = Identity(create_keys=False)
        ident.load_public_key(w.a.identity.get_public_key())
        a_dest = Destination(ident, Destination.OUT, Destination.SINGLE, "lxmf", "delivery")
        m = LXMessage(destination=a_dest, source=w.b_dest, content=content, title="")
        m.pack()
        return m


def _request_sig_pub(w):
    """The sig-pub the reference peer would hold: bytes 32..64 of the link request on the wire."""
    for f in w.request_frames:
        p = parse(f)
        if p.packet_type == const.PKT_LINKREQUEST:
            return p.data[32:64]
    raise AssertionError("no link request seen")


def _b_send_packet(w, link, packed):
    """B sends `packed` to A over `link` as a link packet with a receipt whose proof check is
    the reference's (signature against the link-request sig-pub)."""
    with w.b.ctx():
        pkt = Packet(packet_mod.LinkDestination(link.link_id), link._token.encrypt(packed),
                     const.PKT_DATA, context=const.CTX_NONE, create_receipt=True)
        receipt = pkt.send()
        assert receipt, "B could not send"
        receipt.destination = types.SimpleNamespace(identity=SigPubVerifier(_request_sig_pub(w)))
        return receipt


def _hold_data_proofs(src, frame):
    """Hold B's PROOFs (the proof of A's own message) except the link-request proof."""
    if src.name != "B":
        return False
    p = parse(frame)
    return p.packet_type == const.PKT_PROOF and p.context != const.CTX_LRPROOF


def _start_direct(w, text="hello"):
    with w.a.ctx():
        msg = w.router.send_message(w.b_dest.hash, text, desired_method=LXMessage.DIRECT)
    w.request_frames = []
    # capture the request on its way (World.pump hands frames over directly)
    held = w.pump(hold=lambda src, f: (w.request_frames.append(f) if src is w.a else None,
                                       _hold_data_proofs(src, f))[1])
    return msg, held


def test_x2_own_link_lxmf_packet_is_delivered_once_and_proof_validates_at_the_peer():
    w = World()
    msg, held = _start_direct(w)
    assert msg.state == LXMessage.SENDING and len(w.b_links) == 1, "link up, msg1 unproven yet"
    link_b = w.b_links[0]
    assert w.router.direct_links, "A's own link is still open (msg1 proof is held back)"
    packed = _b_message(w, "reply on your link").packed
    receipt = _b_send_packet(w, link_b, packed)
    held2 = w.pump(hold=_hold_data_proofs)
    with w.a.ctx():
        pass
    assert [m.content_as_string() for m in w.got] == ["reply on your link"], w.got
    assert receipt.status == packet_mod.PacketReceipt.DELIVERED and receipt.proved, \
        "the proof A sent must validate against the sig-pub in the link request"
    # the peer re-sends the identical packet (as a reference peer would after a lost proof)
    receipt2 = _b_send_packet(w, link_b, packed)
    w.pump(hold=_hold_data_proofs)
    assert len(w.got) == 1, "delivered to the app exactly once"
    assert receipt2.status == packet_mod.PacketReceipt.DELIVERED, "a duplicate is still proved"
    # now msg1's own proof arrives
    w.release(held + held2)
    assert msg.state == LXMessage.DELIVERED


def test_x2_resource_arriving_on_our_own_link_is_delivered_once_and_completes_at_the_peer():
    w = World()
    msg, held = _start_direct(w)
    link_b = w.b_links[0]
    big = "x" * 900                                  # does not fit one packet: a real Resource
    packed = _b_message(w, big).packed
    with w.b.ctx():
        res = resource_mod.Resource(link_b, packed, is_response=False)
    w.pump(hold=_hold_data_proofs)
    assert [m.content_as_string() for m in w.got] == [big], [len(m.content_as_string()) for m in w.got]
    assert res.status == resource_mod.COMPLETE, "the peer's Resource must be proved by A (status %r)" % res.status
    w.release(held)
    assert msg.state == LXMessage.DELIVERED


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS " + name)
        except Exception as ex:
            import traceback
            traceback.print_exc()
            print("FAIL " + name + " " + repr(ex))
            failed += 1
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
