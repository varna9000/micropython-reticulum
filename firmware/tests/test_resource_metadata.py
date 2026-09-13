# Metadata-Resource interop tests (host-side, crypto-free). Run:
#   python3 firmware/tests/test_resource_metadata.py
#
# Reference NomadNet nodes serve /media as a *metadata Resource* (RNS 1.5.4):
# serve_media returns [open(file,"rb"), {"name": ...}] -> RNS wraps it in a
# Resource whose payload is prefixed with a metadata blob, and flags the
# advertisement's metadata bit (bit 5 of the flags byte `f`). The blob is
#     struct.pack(">I", len(msgpack(meta)))[1:] + msgpack(meta)      # 3-byte BE len
# prepended to the raw file bytes. The resource hash and proof cover the FULL
# reassembled data (blob + payload); only after hash-verify AND prove does the
# receiver strip the blob and expose the raw payload.
#
# urns must: (1) read has_metadata from flags bit 5 in accept(); (2) strip the
# 3-byte-prefixed blob after prove() in assemble(); (3) in resource_concluded,
# dispatch a metadata response's raw payload directly instead of umsgpack-
# unpacking it as [request_id, data].

import struct
import time
import types
import importlib

import harness
from harness import const, packet, Transport, MockInterface, Identity, reset_transport, link

umsgpack = importlib.import_module("urns.umsgpack")
resource_mod = importlib.import_module("urns.resource")
Resource = resource_mod.Resource
RANDOM_HASH_SIZE = resource_mod.RANDOM_HASH_SIZE
MAPHASH_LEN = resource_mod.MAPHASH_LEN

# The reference wire bit for "this resource carries metadata" (RNS
# ResourceAdvertisement: f = x<<5 | p<<4 | u<<3 | s<<2 | c<<1 | e). Defined
# here from the wire spec, not from any urns constant.
WIRE_METADATA_BIT = 0x20
WIRE_ENCRYPTED_BIT = 0x01
WIRE_IS_RESPONSE_BIT = 0x10

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
    """Identity token: encrypt/decrypt are no-ops so the crafted plaintext is
    also the on-wire ciphertext."""

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


def _rig():
    reset_transport()
    mi = MockInterface("m")
    Transport.interfaces.append(mi)
    return mi, _mklink()


def _frame_metadata(meta_dict):
    """Reference framing: 3-byte big-endian length + msgpack(meta)."""
    packed = umsgpack.packb(meta_dict)
    return struct.pack(">I", len(packed))[1:] + packed


def _build_incoming_resource(ol, payload, meta=None, request_id=None):
    """Craft a receiver-side Resource in the exact reference wire shape.

    Returns (resource, full_data, res_hash). `full_data` is what the sender
    hashes and proves over (blob + payload when meta is given); the resource's
    parts are populated so assemble() can run directly.
    """
    full_data = (_frame_metadata(meta) + payload) if meta is not None else bytes(payload)
    random_hash = b"\x5A" * RANDOM_HASH_SIZE
    res_hash = Identity.full_hash(full_data + random_hash)
    plaintext = random_hash + full_data                # sender encrypts this
    parts = [plaintext]                                # small -> single part
    hashmap = b"".join(
        Identity.full_hash(p + random_hash)[:MAPHASH_LEN] for p in parts
    )
    flags = WIRE_ENCRYPTED_BIT | WIRE_IS_RESPONSE_BIT
    if meta is not None:
        flags |= WIRE_METADATA_BIT
    adv = umsgpack.packb({
        "t": len(plaintext), "d": len(full_data), "n": len(parts),
        "h": res_hash, "r": random_hash, "o": res_hash,
        "i": 1, "l": 1, "q": request_id, "f": flags, "m": hashmap,
    })
    r = Resource.accept(adv, ol)
    if r is None:
        raise RuntimeError("accept() rejected a valid crafted advertisement")
    r.parts = parts
    r.received_count = len(parts)
    r.status = resource_mod.TRANSFERRING
    return r, full_data, res_hash


# --------------------------------------------------------------------------
# accept(): detect metadata from flags bit 5
# --------------------------------------------------------------------------

def test_accept_reads_has_metadata_from_flags_bit_5():
    mi, ol = _rig()
    r_meta, _, _ = _build_incoming_resource(
        ol, b"webp-bytes", meta={"name": b"logo.webp"}, request_id=b"\x01" * 16)
    check(getattr(r_meta, "has_metadata", None) is True,
          "accept sets has_metadata=True when flags bit 5 is set")

    mi2, ol2 = _rig()
    r_plain, _, _ = _build_incoming_resource(
        ol2, b"webp-bytes", meta=None, request_id=b"\x02" * 16)
    check(getattr(r_plain, "has_metadata", None) is False,
          "accept sets has_metadata=False when flags bit 5 is clear")


# --------------------------------------------------------------------------
# assemble(): strip blob AFTER prove(), exposing raw payload
# --------------------------------------------------------------------------

def test_metadata_strip_removes_blob_after_prove():
    """The metadata blob is removed from resource.data so the caller sees the
    raw payload, but the resource proof still covers the FULL transferred data
    (blob + payload) — i.e. the strip runs after prove(), matching reference
    RNS. A strip placed before prove() would send a proof the reference sender
    rejects."""
    mi, ol = _rig()
    payload = b"RIFF\x00\x00\x00\x00WEBPVP8 raw-image-bytes"
    r, full_data, res_hash = _build_incoming_resource(
        ol, payload, meta={"name": b"micropython.webp"}, request_id=None)
    mi.sent = []
    r.assemble()

    check(r.status == resource_mod.COMPLETE,
          "metadata resource assembles to COMPLETE", "status=%r" % r.status)
    check(r.data == payload,
          "metadata blob stripped, raw payload remains", "got %r" % (r.data,))

    proofs = []
    for raw in mi.sent:
        p = packet.Packet(destination=None, data=raw)
        p.unpack()
        if p.context == const.CTX_RESOURCE_PRF:
            proofs.append(p)
    check(len(proofs) == 1, "exactly one resource proof emitted",
          "n=%d" % len(proofs))
    if proofs:
        emitted_proof = proofs[0].data[32:64]        # data = hash(32) + proof(32)
        expected = Identity.full_hash(full_data + res_hash)
        check(emitted_proof == expected,
              "proof covers full metadata+payload (strip runs after prove)",
              "emitted=%s expected=%s"
              % (emitted_proof.hex()[:8], expected.hex()[:8]))


# --------------------------------------------------------------------------
# resource_concluded(): dispatch metadata response as raw payload
# --------------------------------------------------------------------------

def test_metadata_response_dispatched_as_raw_payload():
    mi, ol = _rig()
    rid = b"\x04" * 16
    got = []
    failed = []
    ol.pending_requests[rid] = [
        link.OutgoingLink.REQ_RECEIVING, time.time(), 30,
        lambda r_id, data: got.append((r_id, data)),   # response_callback
        lambda r_id: failed.append(r_id),               # failed_callback
        None,                                           # progress_callback
        None,                                           # max_response_size
    ]
    payload = b"\x89-webp-payload-not-msgpack"
    r, _, _ = _build_incoming_resource(
        ol, payload, meta={"name": b"x.webp"}, request_id=rid)
    r.assemble()

    check(not failed, "metadata response does not fail as malformed",
          "failed=%r" % failed)
    check(len(got) == 1 and got[0][1] == payload,
          "metadata response dispatched as raw payload", "got=%r" % (got,))


def test_plain_response_still_unpacks_rid_data():
    """Regression: a normal (non-metadata) response resource carries
    msgpack([request_id, data]); resource_concluded must keep unpacking it and
    dispatch `data`."""
    mi, ol = _rig()
    rid = b"\x07" * 16
    got = []
    failed = []
    ol.pending_requests[rid] = [
        link.OutgoingLink.REQ_RECEIVING, time.time(), 30,
        lambda r_id, data: got.append((r_id, data)),
        lambda r_id: failed.append(r_id),
        None, None,
    ]
    inner = umsgpack.packb([rid, b"<page>hello</page>"])
    r, _, _ = _build_incoming_resource(ol, inner, meta=None, request_id=rid)
    r.assemble()

    check(not failed, "plain response does not fail", "failed=%r" % failed)
    check(len(got) == 1 and got[0][1] == b"<page>hello</page>",
          "plain [rid, data] response still dispatches data", "got=%r" % (got,))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    total = len(_failures)
    print("\n%d checks failed" % total if total else "\nall metadata-resource tests passed")
    raise SystemExit(1 if _failures else 0)
