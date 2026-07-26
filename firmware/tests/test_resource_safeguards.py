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


# ---------------- request/response size limits (RNS 1.4.1) ------------------
def _mk_inbound_link(max_request_size=None):
    """Inbound (peer-initiated) link whose destination carries an app limit."""
    L = link.Link
    lk = object.__new__(L)
    lk.status = L.ACTIVE
    lk.link_id = LINK_ID
    lk.hash = LINK_ID
    lk._token = _PassToken()
    lk.mtu = 500
    lk.sdu = 465
    lk.mdu = 431
    lk.last_activity = time.time()
    lk.last_outbound = time.time()
    lk.incoming_resources = []
    lk.outgoing_resources = []
    lk.resource_started_callback = None
    lk.resource_concluded_callback = None
    handled = []
    dest = types.SimpleNamespace(hash=DEST, hexhash=DEST.hex(),
                                 max_request_size=max_request_size,
                                 request_handlers={})
    lk.destination = dest
    return lk, dest, handled


def test_max_request_size_rejects_oversized_request():
    reset_transport()
    Transport.interfaces.append(MockInterface("m"))
    lk, dest, _ = _mk_inbound_link(max_request_size=64)
    seen = []
    path_hash = Identity.truncated_hash(b"/big")
    dest.request_handlers[path_hash] = {"path": "/big", "allow": 1,   # ALLOW_ALL
                                        "generator": lambda **kw: seen.append(1)}
    pkt = types.SimpleNamespace(getTruncatedHash=lambda: b"\x01" * 16)

    big = umsgpack.packb([time.time(), path_hash, b"x" * 200])
    lk._handle_request(big, pkt)
    check(seen == [], "oversized request never reaches the handler")

    small = umsgpack.packb([time.time(), path_hash, b"x"])
    check(len(small) <= 64, "control request is inside the limit",
          "%d B" % len(small))
    lk._handle_request(small, pkt)
    check(len(seen) == 1, "request inside the limit is handled")


def test_max_request_size_unset_accepts_anything():
    reset_transport()
    Transport.interfaces.append(MockInterface("m"))
    lk, dest, _ = _mk_inbound_link(max_request_size=None)
    seen = []
    path_hash = Identity.truncated_hash(b"/any")
    dest.request_handlers[path_hash] = {"path": "/any", "allow": 1,   # ALLOW_ALL
                                        "generator": lambda **kw: seen.append(1)}
    pkt = types.SimpleNamespace(getTruncatedHash=lambda: b"\x02" * 16)
    lk._handle_request(umsgpack.packb([time.time(), path_hash, b"x" * 400]), pkt)
    check(len(seen) == 1, "no limit set -> request handled as before")


def test_request_resource_over_limit_cancelled():
    # A request arriving as a resource is capped too, and cancelling before any
    # part transfers sends the receiver-side RCL so the sender stops at once.
    reset_transport()
    mi = MockInterface("m")
    Transport.interfaces.append(mi)
    lk, dest, _ = _mk_inbound_link(max_request_size=100)
    r = _mkresource(lk, False)
    r.request_id = b"\x09" * 16
    r.flags = 0                      # request, not response
    r.total_data_size = 5000
    mi.sent = []
    allowed = lk._request_resource_allowed(r)
    check(not allowed, "oversized request resource rejected")
    check(r.status == resource_mod.FAILED, "rejected resource marked failed")
    check(len(mi.sent) == 1 and mi.sent[0][18] == const.CTX_RESOURCE_RCL,
          "receiver-side RCL signalled to the sender")


def test_delivery_resource_not_capped_by_request_limit():
    # An LXMF delivery carries no request_id and must not be judged by the
    # request limit — otherwise setting one would break normal messaging.
    reset_transport()
    Transport.interfaces.append(MockInterface("m"))
    lk, dest, _ = _mk_inbound_link(max_request_size=100)
    r = _mkresource(lk, False)
    r.request_id = None
    r.flags = 0
    r.total_data_size = 5000
    check(lk._request_resource_allowed(r), "plain delivery passes the request limit")


def test_max_response_size_fails_oversized_single_packet():
    mi, ol = _rig()
    ok, failed = [], []
    ol.pending_requests[b"\x03" * 16] = [
        link.OutgoingLink.REQ_SENT, time.time(), 30,
        lambda rid, data: ok.append(data), lambda rid: failed.append(rid), None,
        16,                                   # max_response_size
    ]
    ol._dispatch_response(b"\x03" * 16, b"y" * 100)
    check(ok == [], "oversized response not delivered to the callback")
    check(failed == [b"\x03" * 16], "requester told the response failed")
    check(b"\x03" * 16 not in ol.pending_requests, "pending request cleared")


def test_max_response_size_allows_within_limit():
    mi, ol = _rig()
    ok = []
    ol.pending_requests[b"\x04" * 16] = [
        link.OutgoingLink.REQ_SENT, time.time(), 30,
        lambda rid, data: ok.append(data), None, None, 1024,
    ]
    ol._dispatch_response(b"\x04" * 16, b"y" * 100)
    check(ok == [b"y" * 100], "response inside the limit is delivered")


# ---------------- RTT-scaled keepalive windows (RNS 1.4.x) ------------------
def test_rtt_scales_keepalive_and_stale_windows():
    lk, _, _ = _mk_inbound_link()
    check(lk.keepalive == link.Link.KEEPALIVE_INTERVAL,
          "defaults to the fixed window before any LRRTT")
    lk._update_rtt(umsgpack.packb(0.1))
    expected = 0.1 * (link.Link.KEEPALIVE_MAX / link.Link.KEEPALIVE_MAX_RTT)
    check(abs(lk.keepalive - expected) < 0.01, "keepalive scaled from rtt",
          "%.2f" % lk.keepalive)
    check(lk.stale_time == lk.keepalive * link.Link.STALE_FACTOR,
          "stale window is twice the keepalive")


def test_rtt_windows_are_clamped():
    lk, _, _ = _mk_inbound_link()
    lk._update_rtt(umsgpack.packb(0.0001))          # absurdly fast
    check(lk.keepalive == link.Link.KEEPALIVE_MIN, "clamped at the floor")
    lk, _, _ = _mk_inbound_link()
    lk._update_rtt(umsgpack.packb(600))             # absurdly slow
    check(lk.keepalive == link.Link.KEEPALIVE_MAX, "clamped at the ceiling")
    check(lk.stale_time == link.Link.STALE_GRACE,
          "ceiling reproduces the old fixed pair")


def test_unusable_rtt_keeps_defaults():
    for payload in (umsgpack.packb(-1), umsgpack.packb("soon"),
                    umsgpack.packb(0), b"\xff\xff not msgpack"):
        lk, _, _ = _mk_inbound_link()
        lk._update_rtt(payload)
        check(lk.keepalive == link.Link.KEEPALIVE_INTERVAL
              and lk.stale_time == link.Link.STALE_GRACE,
              "unusable rtt leaves the defaults (%r)" % payload[:8])


def test_keepalive_reply_throttled_after_recent_send():
    reset_transport()
    mi = MockInterface("m")
    Transport.interfaces.append(mi)
    lk, _, _ = _mk_inbound_link()
    lk._update_rtt(umsgpack.packb(0.1))             # keepalive ~20.6 s
    pkt = types.SimpleNamespace(context=const.CTX_KEEPALIVE, data=b"\xff")

    lk.last_outbound = time.time()                  # we just transmitted
    mi.sent = []
    lk.receive(pkt)
    check(mi.sent == [], "reply skipped while we have recently transmitted")

    lk.last_outbound = time.time() - lk.keepalive - 1
    lk.receive(pkt)
    check(len(mi.sent) == 1, "reply sent once we have gone quiet")
    if mi.sent:
        p = packet.Packet(destination=None, data=mi.sent[0])
        p.unpack()
        check(p.context == const.CTX_KEEPALIVE and p.data == b"\xfe",
              "reply is the 0xFE keepalive answer")
        check(time.time() - lk.last_outbound < 2, "reply stamps last_outbound")


def test_stale_close_uses_derived_window():
    reset_transport()
    Transport.interfaces.append(MockInterface("m"))
    lk, _, _ = _mk_inbound_link()
    lk._update_rtt(umsgpack.packb(0.1))             # stale ~41 s
    lk.last_activity = time.time() - 100            # quiet far past that
    lk.check_keepalive()
    check(lk.status == link.Link.CLOSED, "dead fast link closed on the new window")

    lk2, _, _ = _mk_inbound_link()                  # no LRRTT -> old 720 s
    lk2.last_activity = time.time() - 100
    lk2.check_keepalive()
    check(lk2.status == link.Link.ACTIVE, "link with no rtt keeps the old grace")


def _real_destination_class():
    """harness injects a FAKE urns.destination, so load the real module under a
    side name. The dotted name keeps __package__ == "urns", so its relative
    imports still resolve, and the fake other tests rely on stays in place."""
    import importlib.util
    import os
    fw = os.path.dirname(os.path.dirname(os.path.abspath(harness.__file__)))
    spec = importlib.util.spec_from_file_location(
        "urns._destination_real", os.path.join(fw, "urns", "destination.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Destination


def test_destination_max_request_size_validation():
    d = object.__new__(_real_destination_class())
    d.max_request_size = None
    d.set_max_request_size(2048)
    check(d.max_request_size == 2048, "limit stored")
    d.set_max_request_size(None)
    check(d.max_request_size is None, "None clears the limit")
    try:
        d.set_max_request_size(-1)
        check(False, "negative limit rejected")
    except ValueError:
        check(True, "negative limit rejected")
    try:
        d.set_max_request_size("lots")
        check(False, "non-numeric limit rejected")
    except TypeError:
        check(True, "non-numeric limit rejected")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    total = len(_failures)
    print("\n%d/%d checks failed" % (total, total) if total else "\nall resource safeguard tests passed")
    raise SystemExit(1 if _failures else 0)
