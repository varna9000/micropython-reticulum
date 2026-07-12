# OutgoingLink.request() client API tests (host-side, crypto-free). Run:
#   python3 firmware/tests/test_link_request.py
#
# Uses a pass-through token so the request/response payloads are readable on
# the wire; packet framing, request_id derivation, pending-request state and
# the resource-borne response paths are the real code.

import time
import types
import importlib

import harness
from harness import const, packet, Transport, MockInterface, Identity, reset_transport, link

umsgpack = importlib.import_module("urns.umsgpack")
resource_mod = importlib.import_module("urns.resource")

LINK_ID = b"\x10" * 16
DEST = b"\xD0" * 16


class _PassToken:
    """Pass-through stand-in for crypto.Token — payloads stay readable."""

    def encrypt(self, data):
        return bytes(data)

    def decrypt(self, data):
        return bytes(data)


def _mklink():
    OL = link.OutgoingLink
    ol = object.__new__(OL)
    ol.status = OL.ACTIVE
    ol.link_id = LINK_ID
    ol.hash = LINK_ID
    ol._token = _PassToken()
    ol.mtu = 500
    ol.sdu = 465
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
    return ol


def _rig():
    reset_transport()
    mi = MockInterface("m")
    Transport.interfaces.append(mi)
    return mi, _mklink()


def _fake_rx(context, plaintext):
    """Inbound link packet as OutgoingLink.receive() sees it."""
    return types.SimpleNamespace(context=context, data=plaintext)


class _Calls:
    def __init__(self):
        self.responses = []
        self.failures = []
        self.progress = []

    def on_response(self, rid, data):
        self.responses.append((rid, data))

    def on_failed(self, rid):
        self.failures.append(rid)

    def on_progress(self, resource):
        self.progress.append(resource)


def _adv(rid, d=100, n=2, f=0):
    return umsgpack.packb({
        "t": d, "d": d, "n": n,
        "h": b"\x77" * 32, "r": b"\x66" * resource_mod.RANDOM_HASH_SIZE,
        "o": b"\x77" * 32, "i": 1, "l": 1,
        "q": rid, "f": f,
        "m": b"\x55" * (n * resource_mod.MAPHASH_LEN),
    })


# ----------------------------- wire format --------------------------------
def test_request_wire_format_and_id():
    mi, ol = _rig()
    c = _Calls()
    rid = ol.request("/page/index.mu", response_callback=c.on_response,
                     failed_callback=c.on_failed, timeout=30)
    assert rid is not None and len(rid) == 16
    assert len(mi.sent) == 1
    raw = mi.sent[0]

    # HDR_1 PKT_DATA to a DEST_LINK address, context REQUEST.
    assert raw[0] & 0x03 == const.PKT_DATA
    assert (raw[0] >> 2) & 0x03 == const.DEST_LINK
    assert raw[2:18] == LINK_ID
    assert raw[18] == const.CTX_REQUEST

    # Payload: umsgpack [requested_at, path_hash, data]
    req = umsgpack.unpackb(raw[19:])
    assert isinstance(req, list) and len(req) == 3
    assert req[1] == Identity.truncated_hash(b"/page/index.mu")
    assert req[2] is None

    # request_id must equal the responder's derivation from the wire bytes:
    # truncated hash of (masked first byte + everything after the hop count).
    hashable = bytes([raw[0] & 0x0F]) + raw[2:]
    assert rid == Identity.truncated_hash(hashable)

    # Pending entry registered in SENT state.
    assert ol.pending_requests[rid][0] == link.OutgoingLink.REQ_SENT


def test_request_guards():
    mi, ol = _rig()
    ol.status = link.OutgoingLink.PENDING
    assert ol.request("/page/index.mu", timeout=5) is None
    ol.status = link.OutgoingLink.ACTIVE
    assert ol.request("/x", data=b"\x00" * 600, timeout=5) is None  # > sdu
    assert mi.sent == []
    assert ol.pending_requests == {}


def test_request_default_timeout():
    mi, ol = _rig()
    rid = ol.request("/page/index.mu")
    base = link.OutgoingLink.REQUEST_TIMEOUT_BASE
    per_hop = link.OutgoingLink.REQUEST_TIMEOUT_PER_HOP
    assert ol.pending_requests[rid][2] == base + per_hop  # hops unknown -> 1


# ----------------------------- responses ----------------------------------
def test_single_packet_response():
    mi, ol = _rig()
    c = _Calls()
    rid = ol.request("/page/index.mu", response_callback=c.on_response,
                     failed_callback=c.on_failed, timeout=30)
    ol.receive(_fake_rx(const.CTX_RESPONSE, umsgpack.packb([rid, "PAGE"])))
    assert c.responses == [(rid, "PAGE")]
    assert c.failures == []
    assert ol.pending_requests == {}


def test_unknown_or_malformed_response_ignored():
    mi, ol = _rig()
    c = _Calls()
    rid = ol.request("/page/index.mu", response_callback=c.on_response, timeout=30)
    ol.receive(_fake_rx(const.CTX_RESPONSE, umsgpack.packb([b"\x99" * 16, "X"])))
    ol.receive(_fake_rx(const.CTX_RESPONSE, b"\xc1garbage"))
    ol.receive(_fake_rx(const.CTX_RESPONSE, umsgpack.packb("notalist")))
    assert c.responses == []
    assert rid in ol.pending_requests


def test_resource_response_marks_receiving():
    mi, ol = _rig()
    c = _Calls()
    started = []
    ol.resource_started_callback = lambda r: started.append(r)
    rid = ol.request("/page/index.mu", response_callback=c.on_response,
                     failed_callback=c.on_failed, progress_callback=c.on_progress,
                     timeout=30)
    ol._handle_resource_adv(_adv(rid))
    assert len(ol.incoming_resources) == 1
    r = ol.incoming_resources[0]
    assert r.request_id == rid
    assert r.progress_callback == c.on_progress
    # Pending entry switched to RECEIVING; generic started callback skipped.
    assert ol.pending_requests[rid][0] == link.OutgoingLink.REQ_RECEIVING
    assert started == []

    # Conclude COMPLETE -> response dispatched from resource data.
    r.status = resource_mod.COMPLETE
    r.data = umsgpack.packb([rid, "BIGPAGE"])
    ol.resource_concluded(r)
    assert c.responses == [(rid, "BIGPAGE")]
    assert ol.pending_requests == {}
    assert ol.incoming_resources == []


def test_resource_response_failure_paths():
    mi, ol = _rig()
    c = _Calls()
    rid = ol.request("/a", response_callback=c.on_response,
                     failed_callback=c.on_failed, timeout=30)
    ol._handle_resource_adv(_adv(rid))
    r = ol.incoming_resources[0]
    r.status = resource_mod.FAILED
    ol.resource_concluded(r)
    assert c.failures == [rid]
    assert c.responses == []

    # Malformed resource data on a COMPLETE response also fails the request.
    rid2 = ol.request("/b", response_callback=c.on_response,
                      failed_callback=c.on_failed, timeout=30)
    ol._handle_resource_adv(_adv(rid2))
    r2 = ol.incoming_resources[0]
    r2.status = resource_mod.COMPLETE
    r2.data = umsgpack.packb("junk")
    ol.resource_concluded(r2)
    assert c.failures == [rid, rid2]


def test_oversized_response_fails_fast():
    mi, ol = _rig()
    c = _Calls()
    rid = ol.request("/big", failed_callback=c.on_failed, timeout=30)
    ol._handle_resource_adv(_adv(rid, d=resource_mod.MAX_RESOURCE_SIZE + 1))
    assert c.failures == [rid]
    assert ol.pending_requests == {}
    assert ol.incoming_resources == []


def test_non_response_resource_untouched():
    mi, ol = _rig()
    started = []
    ol.resource_started_callback = lambda r: started.append(r)
    ol._handle_resource_adv(_adv(None))
    assert len(started) == 1  # generic path still fires for plain resources


# ----------------------------- lifecycle ----------------------------------
def test_timeout_expires_sent_only():
    mi, ol = _rig()
    c = _Calls()
    rid1 = ol.request("/slow", failed_callback=c.on_failed, timeout=0.01)
    rid2 = ol.request("/receiving", failed_callback=c.on_failed, timeout=0.01)
    ol.pending_requests[rid2][0] = link.OutgoingLink.REQ_RECEIVING
    ol.pending_requests[rid1][1] -= 1  # sent_at in the past
    ol.pending_requests[rid2][1] -= 1
    ol.last_activity = time.time()
    ol.check_keepalive()
    assert c.failures == [rid1]
    assert rid2 in ol.pending_requests


def test_close_fails_pending():
    mi, ol = _rig()
    c = _Calls()
    closed = []
    ol.closed_callback = lambda l: closed.append(l)
    rid = ol.request("/x", failed_callback=c.on_failed, timeout=30)
    ol._close()
    assert c.failures == [rid]
    assert ol.pending_requests == {}
    assert closed and closed[0] is ol
    # A failed request after close is a no-op.
    assert ol.request("/y", timeout=5) is None


def _run():
    import traceback
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except Exception as e:
            failed += 1
            print("FAIL  " + name + "  ->  " + repr(e))
            traceback.print_exc()
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    return failed


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run() else 0)
