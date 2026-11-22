# Identity.known_ratchets must be bounded like known_destinations (host-side). Run:
#   python3 firmware/tests/test_ratchet_bound.py
#
# Found by a memory-decline hunt on a busy hub (~1 KB/min of free heap lost for an hour): every ratcheted
# destination ever announced added an entry that was never removed (the 30 day expiry is only
# checked on use).
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import announce_stamp_support as sup  # noqa: E402
from harness import const, Transport, reset_transport  # noqa: E402

Identity = sup.RealIdentity


def _h(i):
    return i.to_bytes(4, "big") + b"\x00" * 12


def test_known_ratchets_stay_within_the_known_destinations_cap_and_keep_the_newest():
    reset_transport()
    Identity.known_ratchets.clear()
    clock = [1_000_000]
    real = time.time
    time.time = lambda: clock[0]
    try:
        cap = const.MAX_KNOWN_DESTINATIONS
        for i in range(3 * cap):
            clock[0] += 1
            Identity._remember_ratchet(_h(i), b"r" * 32)
            assert len(Identity.known_ratchets) <= cap, (i, len(Identity.known_ratchets))
        assert len(Identity.known_ratchets) == cap
        assert _h(3 * cap - 1) in Identity.known_ratchets      # newest kept
        assert _h(0) not in Identity.known_ratchets            # oldest evicted
        # a re-announce of a kept destination refreshes it without evicting anything
        clock[0] += 1
        before = set(Identity.known_ratchets)
        Identity._remember_ratchet(_h(3 * cap - 1), b"s" * 32)
        assert set(Identity.known_ratchets) == before
    finally:
        time.time = real
        Identity.known_ratchets.clear()


def test_in_use_peer_keeps_its_ratchet_at_the_cap():
    reset_transport()
    Identity.known_ratchets.clear()
    clock = [2_000_000]
    real = time.time
    time.time = lambda: clock[0]
    try:
        cap = const.MAX_KNOWN_DESTINATIONS
        Identity._remember_ratchet(_h(0), b"r" * 32)           # the oldest ...
        Transport.mark_in_use(_h(0))                           # ... but a conversation peer
        for i in range(1, 2 * cap):
            clock[0] += 1
            Identity._remember_ratchet(_h(i), b"r" * 32)
        assert _h(0) in Identity.known_ratchets
        assert len(Identity.known_ratchets) <= cap + 1
    finally:
        time.time = real
        Identity.known_ratchets.clear()


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for k, v in tests:
        try:
            v()
            print("PASS", k)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print("FAIL", k, repr(e))
            failed += 1
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
