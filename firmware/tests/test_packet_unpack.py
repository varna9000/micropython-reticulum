# Zero-length data-field rejection in Packet.unpack (host-side).
#
# RNS 1.5.1 added an early protocol-violation guard to Packet.unpack(): a frame
# whose data field is empty is malformed and must be dropped up front, rather
# than admitted and left to fail deeper in processing. This is a receiver-side
# local drop with no wire-format impact — no valid RNS packet carries a
# zero-length data field (even a keepalive carries its 1-byte 0xFF/0xFE). A
# well-formed frame with a non-empty data field must still unpack normally.
#
# Run:  python3 firmware/tests/test_packet_unpack.py

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
from harness import packet, build_data_hdr1, build_data_hdr2

DEST = b"\xC0" * 16
TID = b"\xB0" * 16


def _unpack(raw):
    return packet.Packet(None, raw).unpack()


def test_zero_length_data_field_rejected_hdr1():
    # Valid HDR_1 header (flags+hops+dest+context) with an empty data field.
    raw = build_data_hdr1(DEST, ciphertext=b"")
    assert _unpack(raw) is False


def test_zero_length_data_field_rejected_hdr2():
    # Same for a transport-header (HDR_2) frame.
    raw = build_data_hdr2(TID, DEST, ciphertext=b"")
    assert _unpack(raw) is False


def test_normal_data_field_unpacks_hdr1():
    raw = build_data_hdr1(DEST)          # default 16-byte ciphertext
    assert _unpack(raw) is True


def test_normal_data_field_unpacks_hdr2():
    raw = build_data_hdr2(TID, DEST)
    assert _unpack(raw) is True


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("ok " + t.__name__)
        passed += 1
    print("\n%d/%d passed" % (passed, len(tests)))
