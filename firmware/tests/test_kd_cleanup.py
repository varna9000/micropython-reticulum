# Regular known-destination cleanup (host-side, integer clock). Run:
#   python3 firmware/tests/test_kd_cleanup.py
# Modelled on reference RNS 1.6.1 Identity.clean_known_destinations.
import importlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import announce_stamp_support as sup  # noqa: E402
from harness import const, Transport, reset_transport  # noqa: E402

Identity = sup.RealIdentity
T0 = 1_800_000_000
PUB = b"p" * 64


class Clock:
    def __enter__(self):
        self.real = time.time
        self.now = T0
        time.time = lambda: self.now
        reset_transport()
        Identity.known_destinations.clear()
        Identity.known_ratchets.clear()
        Identity._clean_pend = None
        Identity._clean_at = 0
        Identity._kd_saved = 0
        Identity._kd_dirty = False
        Identity.forget_hooks = []
        self.saves = []
        self.real_save = Identity.save_known_destinations
        Identity.save_known_destinations = staticmethod(lambda: self.saves.append(self.now))
        return self

    def __exit__(self, *a):
        time.time = self.real
        Identity.save_known_destinations = self.real_save
        Identity.known_destinations.clear()
        Identity.known_ratchets.clear()
        Identity.forget_hooks = []
        Identity._clean_pend = None

    def run_pass(self, advance=None):
        """Start a pass now and run it to completion; returns total removed."""
        if advance:
            self.now += advance
        Identity._clean_at = 0
        removed = 0
        for _ in range(1000):
            removed += Identity.clean_step(self.now)
            if Identity._clean_pend is None:
                break
        return removed


def _h(i):
    return i.to_bytes(2, "big") * 8


def _add(i, announced, entry_len=5):
    e = [announced, b"", PUB, None, None][:entry_len]
    Identity.known_destinations[_h(i)] = e
    return _h(i)


def test_never_used_pathless_goes_after_the_linger_only():
    with Clock() as c:
        d = _add(1, T0)
        assert c.run_pass(const.KD_UNUSED_LINGER - 1) == 0 and d in Identity.known_destinations
        assert c.run_pass(2) == 1 and d not in Identity.known_destinations


def test_used_one_stays_until_the_long_timeout():
    with Clock() as c:
        d = _add(1, T0)
        Identity.mark_used(d)                       # 6th element appended to a 5-element entry
        assert len(Identity.known_destinations[d]) == 6
        c.run_pass(const.KD_PATH_TIMEOUT)           # long unused but < 1.25x: kept
        assert d in Identity.known_destinations
        assert c.run_pass(const.KD_PATH_TIMEOUT // 4 + 10) == 1
        assert d not in Identity.known_destinations
        # entries built by external code with 4 elements work too
        d4 = _add(2, T0, entry_len=4)
        Identity.mark_used(d4)
        assert Identity.known_destinations[d4][5] == c.now


def test_in_use_own_retained_and_pathed_are_never_removed():
    with Clock() as c:
        in_use, own, kept, pathed, gone = _add(1, T0), _add(2, T0), _add(3, T0), _add(4, T0), _add(5, T0)
        Transport.mark_in_use(in_use)
        class D:
            hash = own
        Transport.destinations.append(D())
        assert Identity.retain_destination(kept)
        assert not Identity.retain_destination(b"x" * 16)
        Transport.reachable_destinations[pathed] = c.now
        c.now += 10                                  # mark_in_use TTL is longer than the linger passes below
        removed = c.run_pass(const.KD_UNUSED_LINGER + 5)
        kd = Identity.known_destinations
        assert gone not in kd and removed >= 1
        assert own in kd and kept in kd and pathed in kd
        # retained survives even the long timeout; unretained becomes a normal used entry
        c.run_pass(const.KD_PATH_TIMEOUT * 2)
        assert kept in kd and own in kd
        Identity.retain_destination(kept, False)
        assert c.run_pass(const.KD_PATH_TIMEOUT * 2) >= 1 and kept not in kd


def test_ratchet_goes_with_destination_expired_and_orphan_ratchets_go():
    with Clock() as c:
        a, b, orphan = _add(1, T0), _add(2, T0), _h(9)
        Identity.known_ratchets[a] = (b"r" * 32, T0)
        Identity.known_ratchets[orphan] = (b"r" * 32, T0)
        Identity.known_ratchets[b] = (b"r" * 32, T0)
        Identity.mark_used(b)
        Transport.reachable_destinations[b] = T0     # b has a path: stays, but its ratchet expires
        c.run_pass(const.KD_UNUSED_LINGER + 5)
        assert a not in Identity.known_destinations and a not in Identity.known_ratchets
        assert orphan not in Identity.known_ratchets
        assert b in Identity.known_ratchets
        Transport.reachable_destinations[b] = c.now
        c.run_pass(const.RATCHET_EXPIRY + 10)
        assert b in Identity.known_destinations and b not in Identity.known_ratchets


def test_work_per_call_is_capped_and_resumes():
    with Clock() as c:
        for i in range(100):
            _add(i, T0)
        c.now += const.KD_UNUSED_LINGER + 5
        Identity._clean_at = 0
        counts = []
        while True:
            counts.append(Identity.clean_step(c.now) if Identity._clean_pend is not None or not counts else 0)
            if Identity._clean_pend is None:
                break
        assert len(counts) == (100 + const.KD_CLEAN_BATCH - 1) // const.KD_CLEAN_BATCH
        assert not Identity.known_destinations
        # between passes a call is a no-op until the interval has passed
        _add(200, c.now - 1000)
        assert Identity.clean_step(c.now + 1) == 0 and _h(200) in Identity.known_destinations


def test_save_only_when_something_was_removed_and_not_more_often_than_the_save_interval():
    with Clock() as c:
        _add(1, T0, 5)
        Identity.mark_used(_h(1))
        c.run_pass(100)                              # nothing removed
        c.run_pass(100)
        assert c.saves == []
        _add(2, c.now - 1000)
        c.run_pass(10)                               # removal, but first save is one interval after the first pass
        assert c.saves == [] and Identity._kd_dirty
        _add(3, c.now - 1000)
        c.run_pass(const.KD_SAVE_INTERVAL)
        assert len(c.saves) == 1 and not Identity._kd_dirty
        _add(4, c.now - 1000)
        c.run_pass(const.KD_CLEAN_INTERVAL)          # removal again, too soon after the save
        assert len(c.saves) == 1


def test_reannounce_keeps_last_used_and_forget_hooks_run():
    with Clock() as c:
        d = _add(1, T0)
        Identity.mark_used(d)
        used = Identity.known_destinations[d][5]
        c.now += 50
        Identity.remember(b"ph", d, PUB, b"app", None)
        assert Identity.known_destinations[d][5] == used and Identity.known_destinations[d][0] == c.now
        seen = []
        Identity.forget_hooks.append(seen.append)
        Identity.known_destinations[d][5] = 0
        c.run_pass(const.KD_UNUSED_LINGER + 100)
        assert seen == [d]


def test_loaded_four_and_five_element_files_still_work():
    with Clock() as c:
        d4, d5 = _add(1, T0, 4), _add(2, T0, 5)
        assert c.run_pass(const.KD_UNUSED_LINGER + 1) == 2


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
