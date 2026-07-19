# Resource/link safeguard tests (host-side, crypto-free). Run:
#   python3 firmware/tests/test_resource_safeguards.py
#
# Covers the hardening ported from reference RNS 1.3.9:
#   - cancellation signalling (RESOURCE_ICL sender / RESOURCE_RCL receiver)
#   - the pre-send link-state guard
#   - advertisement validation (malformed, mistyped, oversized)
#   - identify-once binding on an established link
#   - HDLC frame length validation on the TCP interface

import time
import types
import importlib

import harness
from harness import const, packet, Transport, MockInterface, Identity, reset_transport, link

umsgpack = importlib.import_module("urns.umsgpack")
resource_mod = importlib.import_module("urns.resource")
Resource = resource_mod.Resource

LINK_ID = b"\x20" * 16
DEST = b"\xD1" * 16

_failures = []


def check(cond, name, detail=""):
    if cond:
        print("PASS  " + name)
    else:
        print("FAIL  " + name + ("  ->  " + detail if detail else ""))
        _failures.append(name)


class _PassToken:
    def encrypt(self, data):
        return bytes(data)

    def decrypt(self, data):
        return bytes(data)


def _mklink(status=None):
    OL = link.OutgoingLink
    ol = object.__new__(OL)
    ol.status = OL.ACTIVE if status is None else status
    ol.link_id = LINK_ID
    ol.hash = LINK_ID
    ol._token = _PassToken()
    ol.mtu = 500
    ol.sdu = 465
    ol.mdu = 431
    ol.destination = types.SimpleNamespace(hash=DEST, hexhash=DEST.hex())
    ol.pending_requests = {}
    ol.incoming_resources = []
    ol.outgoing_resources = []
    ol.resource_concluded_callback = None
    ol.resource_started_callback = None
    ol.packet_callback = None
    ol.closed_callback = None
    ol.established_callback = None
    ol.remote_identified_callback = None
    ol.remote_identity = None
    ol.last_activity = time.time()
    ol.request_time = time.time()
    ol.activated_at = time.time()
    ol.establishment_timeout = 60
    ol.rtt = 0
    ol._channel = None
    ol._last_keepalive = time.time()
    return ol


def _rig(status=None):
    reset_transport()
    mi = MockInterface("m")
    Transport.interfaces.append(mi)
    return mi, _mklink(status)


def _mkresource(lnk, is_initiator, status=None):
    """Minimal resource object in a transferring state."""
    r = object.__new__(Resource)
    r.link = lnk
    r.is_initiator = is_initiator
    r.hash = b"\x77" * 32
    r.status = resource_mod.TRANSFERRING if status is None else status
    r.created_at = time.time()
    r.total_parts = 2
    r.received_count = 0
    r.sent_count = 0
    return r


def _adv(**over):
    body = {"t": 100, "d": 100, "n": 2,
            "h": b"\x77" * 32, "r": b"\x66" * resource_mod.RANDOM_HASH_SIZE,
            "o": b"\x77" * 32, "i": 1, "l": 1, "q": None, "f": 0,
            "m": b"\x11\x22\x33\x44" * 2}
    body.update(over)
    return umsgpack.packb(body)


# --------------------------------------------------------------------------
# Cancellation signalling
# --------------------------------------------------------------------------

def test_receiver_cancel_emits_rcl():
    mi, ol = _rig()
    r = _mkresource(ol, is_initiator=False)
    mi.sent = []
    r.cancel()
    check(len(mi.sent) == 1, "receiver cancel emits one packet",
          "sent %d" % len(mi.sent))
    if mi.sent:
        p = packet.Packet(destination=None, data=mi.sent[0])
        p.unpack()
        check(p.context == const.CTX_RESOURCE_RCL,
              "receiver cancel uses RESOURCE_RCL",
              "context=0x%02x" % p.context)
        check(p.destination_hash == LINK_ID, "cancel addressed to the link")


def test_sender_cancel_emits_icl():
    mi, ol = _rig()
    r = _mkresource(ol, is_initiator=True)
    mi.sent = []
    r.cancel()
    check(len(mi.sent) == 1, "sender cancel emits one packet", "sent %d" % len(mi.sent))
    if mi.sent:
        p = packet.Packet(destination=None, data=mi.sent[0])
        p.unpack()
        check(p.context == const.CTX_RESOURCE_ICL,
              "sender cancel uses RESOURCE_ICL", "context=0x%02x" % p.context)


def test_cancel_signal_false_is_silent():
    mi, ol = _rig()
    r = _mkresource(ol, is_initiator=False)
    mi.sent = []
    r.cancel(signal=False)
    check(len(mi.sent) == 0, "cancel(signal=False) sends nothing",
          "sent %d" % len(mi.sent))
    check(r.status == resource_mod.FAILED, "cancel still marks FAILED")


def test_cancel_does_not_loop():
    """A peer-initiated cancel must not bounce a cancel back."""
    mi, ol = _rig()
    r = _mkresource(ol, is_initiator=False)
    ol.incoming_resources.append(r)
    mi.sent = []
    ol._handle_resource_cancel(r.hash)
    check(len(mi.sent) == 0, "inbound cancel does not echo a cancel",
          "sent %d" % len(mi.sent))
    check(r.status == resource_mod.FAILED, "inbound cancel fails the resource")


def test_cancel_on_dead_link_is_safe():
    mi, ol = _rig(status=link.OutgoingLink.CLOSED)
    r = _mkresource(ol, is_initiator=False)
    mi.sent = []
    try:
        r.cancel()
        ok = True
    except Exception as e:
        ok = False
    check(ok, "cancel on closed link does not raise")
    check(len(mi.sent) == 0, "cancel on closed link sends nothing")


# --------------------------------------------------------------------------
# Pre-send link-state guard
# --------------------------------------------------------------------------

def test_link_guard_blocks_send_on_closed_link():
    mi, ol = _rig(status=link.OutgoingLink.CLOSED)
    r = _mkresource(ol, is_initiator=True)
    mi.sent = []
    ok = r._link_ok()
    check(ok is False, "link guard reports closed link")
    check(r.status == resource_mod.FAILED, "link guard cancels the transfer")


def test_link_guard_survives_nulled_link():
    mi, ol = _rig()
    r = _mkresource(ol, is_initiator=True)
    r.link = None          # what Link._close() leaves behind
    try:
        ok = r._link_ok()
        raised = False
    except Exception:
        ok, raised = None, True
    check(not raised, "link guard tolerates a nulled link reference")
    check(ok is False, "nulled link reports not-ok")


def test_link_guard_passes_active_link():
    mi, ol = _rig()
    r = _mkresource(ol, is_initiator=True)
    check(r._link_ok() is True, "link guard passes an active link")
    check(r.status == resource_mod.TRANSFERRING, "active link leaves status alone")


# --------------------------------------------------------------------------
# Advertisement validation
# --------------------------------------------------------------------------

def _accept(adv_bytes, lnk):
    """Resource.accept must never raise, whatever the advertisement holds."""
    try:
        return Resource.accept(adv_bytes, lnk), None
    except Exception as e:
        return None, e


def test_adv_rejects_garbage():
    mi, ol = _rig()
    r, exc = _accept(b"\x00\xff\xfe not msgpack at all", ol)
    check(exc is None, "garbage advertisement does not raise", str(exc))
    check(r is None, "garbage advertisement rejected")


def test_adv_rejects_missing_fields():
    mi, ol = _rig()
    r, exc = _accept(umsgpack.packb({"t": 10}), ol)
    check(exc is None, "advertisement with missing fields does not raise", str(exc))
    check(r is None, "advertisement with missing fields rejected")


def test_adv_rejects_wrong_types():
    mi, ol = _rig()
    r, exc = _accept(_adv(n="lots"), ol)
    check(exc is None, "mistyped advertisement does not raise", str(exc))
    check(r is None, "mistyped advertisement rejected")


def test_adv_rejects_absurd_transfer_size():
    """The MCU killer: a huge claimed size must never reach an allocation."""
    mi, ol = _rig()
    r, exc = _accept(_adv(t=2 ** 31, d=64), ol)
    check(exc is None, "oversized advertisement does not raise", str(exc))
    check(r is None, "oversized transfer size rejected")


def test_adv_rejects_negative_sizes():
    mi, ol = _rig()
    r, exc = _accept(_adv(n=-1), ol)
    check(exc is None, "negative-size advertisement does not raise", str(exc))
    check(r is None, "negative sizes rejected")


def test_adv_accepts_valid():
    mi, ol = _rig()
    r, exc = _accept(_adv(), ol)
    check(exc is None, "valid advertisement does not raise", str(exc))
    check(r is not None, "valid advertisement still accepted")
    if r is not None:
        check(r.total_parts == 2, "valid advertisement parsed", str(r.total_parts))


def test_bad_adv_tears_down_link():
    """Link-level: an unprocessable advertisement closes the link."""
    mi, ol = _rig()

    def boom(*a, **k):
        raise ValueError("hostile")

    saved = Resource.accept
    try:
        resource_mod.Resource.accept = staticmethod(boom)
        ol._handle_resource_adv(b"whatever")
        check(ol.status == link.OutgoingLink.CLOSED,
              "unprocessable advertisement tears the link down",
              "status=%s" % ol.status)
    finally:
        resource_mod.Resource.accept = saved


# --------------------------------------------------------------------------
# Identify-once
# --------------------------------------------------------------------------

def test_identify_binds_once():
    """A second identify must not re-bind the authorisation subject."""
    L = link.Link
    lk = object.__new__(L)
    lk.link_id = LINK_ID
    lk.status = L.ACTIVE
    lk.remote_identity = types.SimpleNamespace(hash=b"\xAA" * 16,
                                               hexhash=(b"\xAA" * 16).hex())
    fired = []
    lk.remote_identified_callback = lambda l, i: fired.append(i)

    keysize, sigsize = 64, 64
    attacker_key = b"\xBB" * keysize
    plaintext = attacker_key + b"\xCC" * sigsize

    real_identity = harness.Identity
    try:
        class _AlwaysValid:
            KEYSIZE = 512      # matches urns.identity.Identity
            SIGLENGTH = 512

            def __init__(self, create_keys=False):
                self.hash = b"\xBB" * 16
                self.hexhash = (b"\xBB" * 16).hex()

            def load_public_key(self, pub):
                pass

            def validate(self, sig, data):
                return True

        idmod = importlib.import_module("urns.identity")
        saved_cls = idmod.Identity
        idmod.Identity = _AlwaysValid
        try:
            lk._handle_identify(plaintext)
        finally:
            idmod.Identity = saved_cls
    finally:
        harness.Identity = real_identity

    check(lk.remote_identity.hash == b"\xAA" * 16,
          "re-identify does not replace the bound identity",
          lk.remote_identity.hash.hex()[:8])
    check(len(fired) == 0, "re-identify does not re-fire the callback",
          "fired %d" % len(fired))


# --------------------------------------------------------------------------
# HDLC frame validation (TCP interface)
# --------------------------------------------------------------------------

def test_hdlc_frame_length_validation():
    tcp = importlib.import_module("urns.interfaces.tcp")
    iface = object.__new__(tcp.TCPClientInterface)
    iface._in_frame = False
    iface._escape = False
    iface._buffer = bytearray()
    iface._frame_overflow = False
    delivered = []
    iface.process_incoming = lambda raw: delivered.append(raw)

    def feed(payload):
        iface._process_byte(tcp.FLAG)
        for b in payload:
            iface._process_byte(b)
        iface._process_byte(tcp.FLAG)

    feed(b"\x01" * 8)                       # shorter than a header
    check(len(delivered) == 0, "undersized HDLC frame dropped",
          "delivered %d" % len(delivered))

    feed(b"\x02" * (tcp.MIN_FRAME_LEN + 12))   # normal frame
    check(len(delivered) == 1, "valid HDLC frame delivered",
          "delivered %d" % len(delivered))

    delivered.clear()
    feed(b"\x03" * (iface.HW_MTU + 64))     # overflows the buffer
    check(len(delivered) == 0, "oversized HDLC frame dropped, not truncated",
          "delivered %d" % len(delivered))

    delivered.clear()
    feed(b"\x04" * (tcp.MIN_FRAME_LEN + 5))
    check(len(delivered) == 1, "reader resynchronises after an oversized frame",
          "delivered %d" % len(delivered))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    total = len(_failures)
    print("\n%d/%d checks failed" % (total, total) if total else "\nall resource safeguard tests passed")
    raise SystemExit(1 if _failures else 0)
