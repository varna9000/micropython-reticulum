# Announce airtime cap tests (host-side, crypto-free).
#
# The cap (Transport._announce_airtime_ok) limits announce REBROADCASTS to
# ANNOUNCE_CAP of an interface's airtime, computed from interface.bitrate.
# These tests cover the semantics that let it engage on LoRa without losing
# announces: a fully-capped pass defers (does not spend the retry), path
# responses bypass the cap entirely, and bitrate declarations on the
# LoRa/TCP interfaces produce the reference values.
#
# Run:  python3 firmware/tests/test_announce_cap.py

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
from harness import (const, Transport, MockInterface, reset_transport,
                     set_identity, build_announce_hdr1, build_announce_data)

DEST = b"\xD0" * 16
RECV_FROM = b"\x01" * 16


def _announce_raw():
    return build_announce_hdr1(DEST, data=build_announce_data(emitted=1000))


def _entry(raw, retries=0, blk_rbrd=False):
    # [ts, rtmo, retries, recv_from, hops, raw, lcl_rbrd, blk_rbrd, attchd_if, recv_if]
    now = time.time()
    return [now, 0, retries, RECV_FROM, 1, raw, 0, blk_rbrd, None, None]


def _setup(iface):
    reset_transport()
    set_identity()
    Transport.interfaces = [iface]


def test_cap_engages_on_low_bitrate():
    iface = MockInterface("lora", bitrate=5469)
    _setup(iface)
    Transport.announce_table[DEST] = _entry(_announce_raw())

    Transport._service_announce_table()
    assert len(iface.sent) == 1, "first rebroadcast must pass"
    assert Transport.announce_table[DEST][const.IDX_AT_RETRIES] == 1
    assert iface._announce_allowed_at > time.time(), "bucket must be closed"


def test_capped_pass_defers_without_spending_retry():
    iface = MockInterface("lora", bitrate=5469)
    _setup(iface)
    Transport.announce_table[DEST] = _entry(_announce_raw())

    Transport._service_announce_table()          # sends, closes the bucket
    entry = Transport.announce_table[DEST]
    entry[const.IDX_AT_RTMO] = 0                 # force another pass now
    Transport._service_announce_table()          # bucket closed -> capped

    assert len(iface.sent) == 1, "capped pass must not transmit"
    assert entry[const.IDX_AT_RETRIES] == 1, "capped pass must not spend a retry"
    assert entry[const.IDX_AT_RTMO] >= iface._announce_allowed_at, \
        "entry must be rescheduled to when the bucket opens"

    # Bucket opens -> the SAME retry budget still delivers the announce.
    iface._announce_allowed_at = 0
    entry[const.IDX_AT_RTMO] = 0
    Transport._service_announce_table()
    assert len(iface.sent) == 2, "deferred announce must go out once bucket opens"
    assert entry[const.IDX_AT_RETRIES] == 2


def test_mixed_interfaces_spend_retry_when_one_sends():
    slow = MockInterface("lora", bitrate=5469)
    fast = MockInterface("tcp", bitrate=10_000_000)
    _setup(slow)
    Transport.interfaces = [slow, fast]
    slow._announce_allowed_at = time.time() + 999   # slow bucket closed
    Transport.announce_table[DEST] = _entry(_announce_raw())

    Transport._service_announce_table()
    assert len(slow.sent) == 0 and len(fast.sent) == 1
    assert Transport.announce_table[DEST][const.IDX_AT_RETRIES] == 1, \
        "a pass that transmitted anywhere spends the retry"


def test_path_response_bypasses_cap():
    iface = MockInterface("lora", bitrate=5469)
    _setup(iface)
    iface._announce_allowed_at = time.time() + 999   # bucket firmly closed
    # Path-response entry shape (_enqueue_path_response): retries preloaded,
    # blk_rbrd=True, fires on next tick.
    Transport.announce_table[DEST] = _entry(
        _announce_raw(), retries=const.PATHFINDER_R, blk_rbrd=True)

    Transport._service_announce_table()
    assert len(iface.sent) == 1, "path response must bypass the airtime cap"


def test_zero_bitrate_never_capped():
    iface = MockInterface("mock", bitrate=0)
    _setup(iface)
    Transport.announce_table[DEST] = _entry(_announce_raw())
    Transport._service_announce_table()
    entry = Transport.announce_table[DEST]
    entry[const.IDX_AT_RTMO] = 0
    Transport._service_announce_table()
    assert len(iface.sent) == 2, "bitrate=0 disables the cap entirely"


def test_lora_bitrate_formula():
    from urns.interfaces.lora import LoRaInterface
    # SF7/125kHz/CR5 and SF11/125kHz/CR5 — reference RNodeInterface values.
    i7 = LoRaInterface({"sf": 7, "bw": "125", "coding_rate": 5})
    assert abs(i7.bitrate - 5468.75) < 0.01, i7.bitrate
    i11 = LoRaInterface({"sf": 11, "bw": "125", "coding_rate": 5})
    assert abs(i11.bitrate - 537.109375) < 0.01, i11.bitrate
    # Explicit override wins; 0 disables.
    i0 = LoRaInterface({"sf": 7, "bw": "125", "coding_rate": 5, "bitrate": 0})
    assert i0.bitrate == 0


def test_announce_airtime_ok_math():
    iface = MockInterface("lora", bitrate=5469)
    assert Transport._announce_airtime_ok(iface, 180) is True
    # 180 B at 5469 bps: tx ~0.263 s -> next allowed ~13.2 s out at 2% cap.
    gap = iface._announce_allowed_at - time.time()
    assert 12.0 < gap < 14.5, gap
    assert Transport._announce_airtime_ok(iface, 180) is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("ok " + t.__name__)
        passed += 1
    print("\n%d/%d passed" % (passed, len(tests)))
