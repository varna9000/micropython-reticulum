# Excessive-hop-count rejection in Transport.packet_filter (host-side).
#
# RNS 1.5.0 "improved early rejection of packets with excessive hop counts":
# a packet that reaches the absolute hop ceiling (const.PATHFINDER_M = 128) is
# looping or hostile and is dropped up front, before any other admission logic
# — including the link/resource sub-packet whitelist. This is a receiver-side
# local drop with no wire-format impact. The pre-existing PLAIN/GROUP single-
# hop cap must remain intact.
#
# Run:  python3 firmware/tests/test_packet_filter.py

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
from harness import const, packet, Transport, reset_transport, build_data_hdr1

DEST = b"\xC0" * 16


def _filter(raw):
    p = packet.Packet(None, raw)
    assert p.unpack(), "packet failed to unpack"
    return Transport.packet_filter(p)


def _data(hops, context=0x00, dest_type=0x00):
    return build_data_hdr1(DEST, hops=hops, context=context, dest_type=dest_type)


def test_excessive_hops_dropped():
    reset_transport()
    assert _filter(_data(hops=200)) is False
    assert _filter(_data(hops=const.PATHFINDER_M)) is False   # exactly at the ceiling


def test_just_below_ceiling_passes():
    reset_transport()
    # PATHFINDER_M - 1 = 127 hops is under the ceiling; a fresh SINGLE data
    # packet is admitted by the dedup path.
    assert _filter(_data(hops=const.PATHFINDER_M - 1)) is True


def test_ceiling_beats_resource_whitelist():
    # A resource sub-packet is normally whitelisted (returns True regardless of
    # dedup); an excessive hop count must still drop it, proving the guard runs
    # before the whitelist.
    reset_transport()
    assert _filter(_data(hops=1, context=const.CTX_RESOURCE)) is True
    reset_transport()
    assert _filter(_data(hops=200, context=const.CTX_RESOURCE)) is False


def test_normal_single_data_passes():
    reset_transport()
    assert _filter(_data(hops=1)) is True


def test_plain_group_single_hop_cap_intact():
    # Regression: the pre-existing PLAIN/GROUP one-hop cap is unchanged.
    reset_transport()
    assert _filter(_data(hops=1, dest_type=const.DEST_PLAIN)) is True
    reset_transport()
    assert _filter(_data(hops=2, dest_type=const.DEST_PLAIN)) is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("ok " + t.__name__)
        passed += 1
    print("\n%d/%d passed" % (passed, len(tests)))
