# Host-side tests for the web monitor's announced-name cache: names are learned
# from live announces (RAM only), keyed by destination hash AND announcer
# identity hash, and joined into the /api path rows (dname/vname).
#
# Run:  python3 firmware/tests/test_webmonitor.py

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401  (installs shims + synthetic urns package)
from harness import (Transport, MockInterface, Identity, reset_transport,
                     build_announce_hdr1, build_announce_hdr2,
                     build_announce_data)

# webmonitor.py lives in firmware/ root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gc
gc.mem_free = lambda: 0          # CPython shim for the snapshot's memory pill
import webmonitor  # noqa: E402
from urns import umsgpack  # noqa: E402

ALICE = b"\xD0" * 16             # remote LXMF peer (behind the relay)
RELAY_DEST = b"\xC0" * 16        # a destination announced by the relay itself
RELAY_PUB = b"\x77" * 64
RELAY_ID = Identity.truncated_hash(RELAY_PUB)   # what shows up in `via`


def _fresh():
    reset_transport()
    webmonitor._NAMES.clear()
    webmonitor._PROTOS.clear()
    Identity.known_destinations = {}
    iface = MockInterface("lora")
    Transport.interfaces = [iface]
    Transport.register_announce_handler(webmonitor._on_announce)
    return iface


def test_direct_path_named_both_columns():
    iface = _fresh()
    # LXMF 0.5+ app_data: msgpack [display_name, stamp_cost]
    Identity.app_data[ALICE] = umsgpack.packb([b"Alice", 0])
    Transport.inbound(build_announce_hdr1(
        ALICE, data=build_announce_data(emitted=1000)), iface)

    row = [p for p in webmonitor._snapshot()["paths"]
           if p["dest"] == ALICE.hex()][0]
    assert row["dname"] == "Alice"
    assert row["vname"] == "Alice"          # direct path: via == dest


def test_relayed_path_via_column_shows_relay_name():
    iface = _fresh()
    # The relay announces its own destination (legacy raw-string app_data,
    # like a NomadNet node name) -> maps its identity hash to the name.
    Identity.app_data[RELAY_DEST] = b"RouterBob"
    Transport.inbound(build_announce_hdr1(
        RELAY_DEST, data=build_announce_data(emitted=1000, pubkey=RELAY_PUB)), iface)
    assert webmonitor._NAMES[RELAY_ID] == "RouterBob"

    # A peer behind the relay: HDR_2 announce stamped with the relay's id.
    Identity.app_data[ALICE] = umsgpack.packb([b"Alice", 0])
    Transport.inbound(build_announce_hdr2(
        RELAY_ID, ALICE, data=build_announce_data(emitted=1001), hops=2), iface)

    row = [p for p in webmonitor._snapshot()["paths"]
           if p["dest"] == ALICE.hex()][0]
    assert row["dname"] == "Alice"
    assert row["via"] == RELAY_ID.hex()[:8]
    assert row["vname"] == "RouterBob"


def test_unnamed_announce_adds_no_name_keys():
    iface = _fresh()
    Transport.inbound(build_announce_hdr1(
        ALICE, data=build_announce_data(emitted=1000)), iface)   # no app_data

    row = [p for p in webmonitor._snapshot()["paths"]
           if p["dest"] == ALICE.hex()][0]
    assert "dname" not in row and "vname" not in row
    assert webmonitor._NAMES == {}


def test_name_cache_bounded_and_truncated():
    _fresh()
    for i in range(webmonitor._MAX_NAMES + 8):
        webmonitor._note_name(i.to_bytes(16, "big"), "n" + str(i))
    assert len(webmonitor._NAMES) <= webmonitor._MAX_NAMES
    # Long announced names are capped at 32 chars by _on_announce.
    class _P:
        data = b"\x11" * 64
    webmonitor._on_announce(ALICE, b"x" * 100, _P())
    assert webmonitor._NAMES[ALICE] == "x" * 32


NH_LXMF = Identity.full_hash(b"lxmf.delivery")[:10]
NH_NOMAD = Identity.full_hash(b"nomadnetwork.node")[:10]


def test_proto_from_announce_name_hash():
    iface = _fresh()
    Transport.inbound(build_announce_hdr1(
        ALICE, data=build_announce_data(emitted=1000, name_hash=NH_LXMF)), iface)
    NODE = b"\xD1" * 16
    Transport.inbound(build_announce_hdr1(
        NODE, data=build_announce_data(emitted=1001, name_hash=NH_NOMAD)), iface)

    rows = {p["dest"]: p for p in webmonitor._snapshot()["paths"]}
    assert rows[ALICE.hex()]["proto"] == "lxmf"
    assert rows[NODE.hex()]["proto"] == "nomad"


def test_proto_unknown_app_shows_hex_tag():
    iface = _fresh()
    Transport.inbound(build_announce_hdr1(
        ALICE, data=build_announce_data(emitted=1000, name_hash=b"\x99" * 10)), iface)
    row = [p for p in webmonitor._snapshot()["paths"]
           if p["dest"] == ALICE.hex()][0]
    assert row["proto"] == "?" + (b"\x99" * 10).hex()[:6]


def test_proto_backfill_from_persisted_identity():
    """After a reboot _PROTOS is empty but paths/identities persist — the
    label is recomputed as H(candidate_name_hash + identity_hash) == dest."""
    iface = _fresh()
    pub = b"\x55" * 64
    id_hash = Identity.truncated_hash(pub)
    dest = Identity.full_hash(NH_LXMF + id_hash)[:16]
    Transport.inbound(build_announce_hdr1(
        dest, data=build_announce_data(emitted=1000, pubkey=pub)), iface)

    webmonitor._PROTOS.clear()                       # "reboot"
    Identity.known_destinations = {dest: [0, None, pub, None]}
    row = [p for p in webmonitor._snapshot()["paths"]
           if p["dest"] == dest.hex()][0]
    assert row["proto"] == "lxmf"
    assert webmonitor._PROTOS[dest] == "lxmf"        # cached, no recompute

    # Unknown-app identity caches a definitive "?" miss.
    webmonitor._PROTOS.clear()
    other = Identity.full_hash(b"\x42" * 10 + id_hash)[:16]
    Identity.known_destinations[other] = [0, None, pub, None]
    assert webmonitor._classify(other) == "?"


def test_proto_none_when_identity_unknown():
    _fresh()
    assert webmonitor._classify(b"\x77" * 16) is None


# ------------------------------- runner ----------------------------------
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
    sys.exit(1 if _run() else 0)
