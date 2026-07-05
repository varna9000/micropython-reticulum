# Host-side tests for RNode split-frame reassembly in urns/interfaces/lora.py,
# including hardening against transparent repeaters that re-transmit every
# frame verbatim (same seq -> naive reassembly appends a copied frame 1 as
# "frame 2" and corrupts every split packet).
#
# Run:  python3 firmware/tests/test_lora_split.py

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401  (installs shims + synthetic urns package)

from urns.interfaces.lora import LoRaInterface, _FRAME_PAYLOAD  # noqa: E402


def make_iface():
    i = LoRaInterface({"name": "test"})   # offline on host; RX path needs no modem
    return i


def frame(seq, payload, split=True):
    hdr = ((seq & 0x0F) << 4) | (0x01 if split else 0x00)
    return bytes([hdr]) + payload


F1 = bytes(range(250)) + b"ABCD"          # 254B full first fragment
F2 = b"tail-of-packet" * 3                # 45B second fragment


def test_non_split_passthrough():
    i = make_iface()
    assert i._rx_frame(frame(7, b"hello", split=False)) == b"hello"


def test_runt_frame_ignored():
    i = make_iface()
    assert i._rx_frame(b"\x01") is None
    assert i._rx_frame(b"") is None


def test_normal_split_reassembly():
    i = make_iface()
    assert i._rx_frame(frame(3, F1)) is None
    assert i._rx_frame(frame(3, F2)) == F1 + F2


def test_repeater_echo_sequence():
    # On-air order with a transparent repeater + LBT senders:
    # f1, f1' (copy), f2, f2' (copy) -> exactly one clean packet.
    i = make_iface()
    assert i._rx_frame(frame(9, F1)) is None          # f1 buffered
    assert i._rx_frame(frame(9, F1)) is None          # f1' dup -> dropped
    assert i._rx_frame(frame(9, F2)) == F1 + F2       # f2 completes
    assert i._rx_frame(frame(9, F2)) is None          # f2' dup of done -> dropped
    assert i._rx_frame(frame(9, F1)) is None          # late f1' -> dropped (done fp)
    assert i._reasm_buf is None                       # nothing left dangling


def test_stray_tail_while_idle_dropped():
    i = make_iface()
    assert i._rx_frame(frame(5, F2)) is None          # short first-seen: stray tail
    assert i._reasm_buf is None                       # NOT buffered as frame 1
    # normal split still works afterwards
    assert i._rx_frame(frame(5, F1)) is None
    assert i._rx_frame(frame(5, F2)) == F1 + F2


def test_new_seq_replaces_stale():
    i = make_iface()
    other = bytes(reversed(range(254)))
    assert i._rx_frame(frame(1, F1)) is None          # A pending
    assert i._rx_frame(frame(2, other)) is None       # B replaces A
    assert i._rx_frame(frame(2, F2)) == other + F2


def test_same_seq_back_to_back_not_blocked_by_dup_window():
    # A second, different split packet reusing the same seq right after one
    # completed must still go through (fingerprint match requires same bytes).
    i = make_iface()
    assert i._rx_frame(frame(4, F1)) is None
    assert i._rx_frame(frame(4, F2)) == F1 + F2
    g1 = bytes([x ^ 0xFF for x in F1])
    g2 = b"different-tail"
    assert i._rx_frame(frame(4, g1)) is None
    assert i._rx_frame(frame(4, g2)) == g1 + g2


def test_max_size_508_with_echoes():
    # 508B packet: both halves full 254B; copies of either half dropped.
    i = make_iface()
    h1 = bytes([1]) * _FRAME_PAYLOAD
    h2 = bytes([2]) * _FRAME_PAYLOAD
    assert i._rx_frame(frame(11, h1)) is None
    assert i._rx_frame(frame(11, h2)) == h1 + h2
    assert i._rx_frame(frame(11, h2)) is None         # f2' copy dropped
    assert i._rx_frame(frame(11, h1)) is None         # f1' copy dropped
    assert i._reasm_buf is None


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
