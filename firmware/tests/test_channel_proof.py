# End-to-end proof/ACK integration test for Channel over a (mostly) REAL urns
# stack under CPython: real Ed25519 identities, real Packet hashing, real
# Transport proof routing + PacketReceipt validation, real Channel + link
# methods. Only AES (ucryptolib) is stubbed and bypassed via a passthrough
# link Token — the proof mechanism is Ed25519 + SHA-256, orthogonal to AES.
#
# Validates the two load-bearing directions of the Channel ACK:
#   A. the listener PROVES our sent CHANNEL packet -> our receipt validates the
#      proof via _ChannelDestination.identity -> Channel marks it delivered.
#   B. we PROVE the listener's CHANNEL packet with our ephemeral key -> the
#      signature validates against the sig-pub we would put in the link request.
#
# Run:  python3 firmware/tests/test_channel_proof.py

import os
import sys
import types
import hashlib
import asyncio
import importlib


def _bootstrap():
    mp = types.ModuleType("micropython")
    mp.const = lambda x: x
    mp.native = lambda f: f
    mp.viper = lambda f: f
    sys.modules["micropython"] = mp
    sys.modules.setdefault("uhashlib", hashlib)
    sys.modules.setdefault("uasyncio", asyncio)
    sys.modules.setdefault("machine", types.ModuleType("machine"))
    sys.modules.setdefault("os", os)

    # ucryptolib stub — present so urns.crypto imports; never actually called
    # because the test link uses a passthrough Token.
    if "ucryptolib" not in sys.modules:
        uc = types.ModuleType("ucryptolib")

        class _Aes:
            def __init__(self, *a):
                raise RuntimeError("AES not available under CPython (test uses passthrough token)")

        uc.aes = _Aes
        sys.modules["ucryptolib"] = uc

    fw = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    urns_dir = os.path.join(fw, "urns")
    import importlib.machinery
    import importlib.util
    spec = importlib.machinery.ModuleSpec("urns", loader=None, is_package=True)
    spec.submodule_search_locations = [urns_dir]
    pkg = importlib.util.module_from_spec(spec)
    pkg.__path__ = [urns_dir]
    sys.modules["urns"] = pkg


_bootstrap()
const = importlib.import_module("urns.const")
logmod = importlib.import_module("urns.log")
logmod.set_loglevel(logmod.LOG_NONE)
Identity = importlib.import_module("urns.identity").Identity
packet_mod = importlib.import_module("urns.packet")
transport_mod = importlib.import_module("urns.transport")
channel = importlib.import_module("urns.channel")
link_mod = importlib.import_module("urns.link")
from urns.crypto import Ed25519PrivateKey

Transport = transport_mod.Transport
Packet = packet_mod.Packet
LinkDestination = packet_mod.LinkDestination
OutgoingLink = link_mod.OutgoingLink


class PassToken:
    def encrypt(self, data):
        return bytes(data)

    def decrypt(self, data):
        return bytes(data)


class MockInterface:
    def __init__(self, name="m"):
        self.name = name
        self.online = True
        self.OUT = True
        self.IN = True
        self.HW_MTU = 500
        self.bitrate = 10000
        self.ifac_signing_key = None
        self.ifac_key = None
        self.ifac_size = 0
        self.rssi = None
        self.snr = None
        self.sent = []

    def process_outgoing(self, data):
        self.sent.append(bytes(data))
        return True

    def __str__(self):
        return self.name


class Ping(channel.MessageBase):
    MSGTYPE = 0x0101

    def __init__(self, data=b""):
        self.data = data

    def pack(self):
        return self.data

    def unpack(self, raw):
        self.data = raw


def _reset(server_id):
    Transport.interfaces = []
    Transport.destinations = []
    Transport.active_links = []
    Transport.pending_links = []
    Transport.receipts = []
    Transport.packet_hashlist = set()
    Transport.packet_hashlist_prev = set()
    Transport.path_table = {}
    Transport.reverse_table = {}
    Transport.link_table = {}
    Transport.reachable_destinations = {}
    Transport.blackholed_identities = []
    Transport.identity = server_id      # any identity with a .hash
    iface = MockInterface()
    Transport.interfaces.append(iface)
    return iface


def _mklink(server_id, sign_proofs=True):
    """A real OutgoingLink, built past __init__ (no ECDH), with a passthrough
    token and — for proofs — a real ephemeral Ed25519 key."""
    import time
    ol = object.__new__(OutgoingLink)
    ol.status = OutgoingLink.ACTIVE
    ol.link_id = os.urandom(16)
    ol.hash = ol.link_id
    ol._token = PassToken()
    ol.mtu = 500
    ol.sdu = 465
    ol.mdu = 431
    ol.rtt = 2.0
    ol.destination = types.SimpleNamespace(identity=server_id, hash=server_id.hash)
    ol.incoming_resources = []
    ol.outgoing_resources = []
    ol.pending_requests = {}
    ol.resource_concluded_callback = None
    ol.resource_started_callback = None
    ol.packet_callback = None
    ol.remote_identity = None
    ol.last_activity = time.time()
    ol._channel = None
    if sign_proofs:
        ol._sig_prv = Ed25519PrivateKey.generate()
        ol._sig_pub_bytes = ol._sig_prv.public_key().public_bytes()
    else:
        ol._sig_prv = None
    return ol


def test_A_listener_proof_acks_our_send():
    """We send a CHANNEL packet; the listener proves it; our Channel advances."""
    server_id = Identity()
    iface = _reset(server_id)
    link = _mklink(server_id)

    ch = link.get_channel()
    ch.register_message_type(Ping)
    assert ch.window == 1                 # rtt 2.0 > RTT_SLOW
    assert ch.is_ready_to_send()

    ch.send(Ping(b"hello world"))
    assert not ch.is_ready_to_send()      # one outstanding, window 1
    assert len(iface.sent) == 1
    assert len(Transport.receipts) == 1

    # Parse what went on the wire, as the listener would.
    wire = iface.sent[0]
    p = Packet(destination=None, data=wire)
    assert p.unpack()
    assert p.context == const.CTX_CHANNEL
    assert p.destination_hash == link.link_id

    # Listener proves it: explicit proof = packet_hash + sign(packet_hash).
    proof_data = p.packet_hash + server_id.sign(p.packet_hash)
    proof = Packet(LinkDestination(link.link_id), proof_data,
                   const.PKT_PROOF, create_receipt=False)
    proof.pack()

    # Deliver the proof through the real inbound path.
    Transport.inbound(proof.raw, iface)

    assert Transport.receipts[0].status == packet_mod.PacketReceipt.DELIVERED
    assert ch.is_ready_to_send()          # window freed -> ACK worked
    assert len(ch._tx_ring) == 0
    print("ok test_A_listener_proof_acks_our_send")


def test_A_bad_proof_rejected():
    """A proof signed by the WRONG identity must not ACK our send."""
    server_id = Identity()
    wrong_id = Identity()
    iface = _reset(server_id)
    link = _mklink(server_id)
    ch = link.get_channel()
    ch.register_message_type(Ping)
    ch.send(Ping(b"x"))

    p = Packet(destination=None, data=iface.sent[0])
    p.unpack()
    proof_data = p.packet_hash + wrong_id.sign(p.packet_hash)   # wrong signer
    proof = Packet(LinkDestination(link.link_id), proof_data,
                   const.PKT_PROOF, create_receipt=False)
    proof.pack()
    Transport.inbound(proof.raw, iface)

    assert Transport.receipts[0].status == packet_mod.PacketReceipt.SENT  # NOT delivered
    assert not ch.is_ready_to_send()
    print("ok test_A_bad_proof_rejected")


def test_B_we_prove_listener_packet():
    """The listener sends us a CHANNEL packet; we deliver it and prove it with
    our ephemeral key; the proof validates against our link-request sig-pub."""
    server_id = Identity()
    iface = _reset(server_id)
    link = _mklink(server_id)
    Transport.active_links.append(link)

    ch = link.get_channel()
    ch.register_message_type(Ping)
    got = []
    ch.add_message_handler(lambda m: got.append(m.data) or False)

    # Build the CHANNEL packet the listener would send us (seq 0, passthrough
    # token so the "ciphertext" is the raw envelope).
    env = channel.Envelope(None, message=Ping(b"shell output"), sequence=0)
    envelope_bytes = env.pack()
    incoming = Packet(LinkDestination(link.link_id), envelope_bytes,
                      const.PKT_DATA, context=const.CTX_CHANNEL, create_receipt=False)
    incoming.pack()
    parsed = Packet(destination=None, data=incoming.raw)
    parsed.unpack()

    link.receive(parsed)   # -> _handle_channel -> prove_packet + channel._receive

    # The envelope was delivered to the handler.
    assert got == [b"shell output"], got

    # We emitted a proof; validate it against our ephemeral sig-pub, as the
    # listener would (Ed25519PublicKey.from_public_bytes(sig_pub).verify).
    from urns.crypto import Ed25519PublicKey
    proofs = [r for r in iface.sent
              if Packet(destination=None, data=r) and _is_proof_for(r, parsed.packet_hash)]
    assert proofs, "no matching proof emitted"
    proof_raw = proofs[-1]
    pp = Packet(destination=None, data=proof_raw)
    pp.unpack()
    sig = pp.data[32:]                       # explicit proof: hash(32) + sig(64)
    pub = Ed25519PublicKey.from_public_bytes(link._sig_pub_bytes)
    pub.verify(sig, parsed.packet_hash)      # raises on failure
    print("ok test_B_we_prove_listener_packet")


def _is_proof_for(raw, packet_hash):
    try:
        p = Packet(destination=None, data=raw)
        if not p.unpack():
            return False
        return p.packet_type == const.PKT_PROOF and p.data[:32] == packet_hash
    except Exception:
        return False


if __name__ == "__main__":
    for name in list(globals()):
        if name.startswith("test_"):
            globals()[name]()
    print("all channel-proof tests passed")
