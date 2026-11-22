# Announce emission time (destination.py _emission_time): no drift, bounded
# flash writes, strictly increasing across reboots, RTC-memory carry.
# Integer-second clock (IntClock from test_review_fixes2) drives time.time().
import builtins
import os
import random
import shutil
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import announce_stamp_support as S
from test_review_fixes2 import IntClock

D = S.RealDestination
RealIdentity = S.RealIdentity
H = b"\x55" * 16
T0 = 1_795_285_656               # a valid wall clock
UNSET = 946684800 + 10           # ESP32 unset clock after the 2000->1970 conversion


class FakeRTC:
    mem = b""
    working = True

    def memory(self, data=None):
        if not FakeRTC.working:
            raise OSError("no rtc memory")
        if data is None:
            return FakeRTC.mem
        assert len(data) <= 2048
        FakeRTC.mem = bytes(data)


class Rig:
    def __init__(self, rtc=True):
        self.tmp = tempfile.mkdtemp()
        self.old_sp = RealIdentity.storagepath
        RealIdentity.storagepath = self.tmp
        self.writes = 0
        self.real_open = builtins.open
        rig = self

        def counting_open(path, mode="r", *a, **k):
            if "w" in mode and "/emitted/" in str(path):
                rig.writes += 1
            return rig.real_open(path, mode, *a, **k)
        builtins.open = counting_open
        self.mod = sys.modules.setdefault("machine", types.ModuleType("machine"))
        self.old_rtc = getattr(self.mod, "RTC", None)
        self.mod.RTC = FakeRTC
        FakeRTC.mem = b""
        FakeRTC.working = rtc
        self.clock = IntClock(T0 * 1000)

    def boot(self, power_loss=False):
        D._emitted.clear()
        D._stored.clear()
        if power_loss:
            FakeRTC.mem = b""

    def emit(self, now=None):
        return D._emission_time(H, self.clock.ms // 1000 if now is None else now)

    def close(self):
        self.clock.close()
        builtins.open = self.real_open
        RealIdentity.storagepath = self.old_sp
        if self.old_rtc is None:
            del self.mod.RTC
        else:
            self.mod.RTC = self.old_rtc
        D._emitted.clear()
        D._stored.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)


def _with_rig(rtc=True):
    def deco(fn):
        def run():
            r = Rig(rtc)
            try:
                fn(r)
            finally:
                r.close()
        run.__name__ = fn.__name__
        return run
    return deco


@_with_rig(rtc=True)
def test_deep_sleep_loop_valid_clock_emits_real_time_with_bounded_writes(r):
    seen = []
    for i in range(200):                 # wake every minute: boot, announce, sleep
        r.boot()
        r.clock.ms += 60_000
        t = r.emit()
        assert t == r.clock.ms // 1000, (i, t - r.clock.ms // 1000)   # no drift
        seen.append(t)
    assert seen == sorted(set(seen))
    assert r.writes <= 1 + 200 * 60 // D.PERSIST_EVERY, r.writes


@_with_rig(rtc=False)
def test_flash_only_power_cycle_valid_clock_no_drift_bounded_writes(r):
    for i in range(200):
        r.boot(power_loss=True)
        r.clock.ms += 60_000
        t = r.emit()
        assert t == r.clock.ms // 1000, (i, t - r.clock.ms // 1000)
    assert r.writes <= 1 + 200 * 60 // D.PERSIST_EVERY, r.writes


@_with_rig(rtc=True)
def test_unset_clock_strictly_increasing_across_reboots_bounded_writes(r):
    last = r.emit(T0)                    # an earlier boot had a real clock
    r.boot()
    base = r.writes
    for i in range(200):
        r.boot()
        t = r.emit(UNSET + i)
        assert t > last, (i, t, last)
        last = t
        t = r.emit(UNSET + i + 1)
        assert t > last
        last = t
    assert r.writes - base <= 2, r.writes - base


@_with_rig(rtc=False)
def test_unset_clock_flash_only_never_reuses_a_value(r):
    last = r.emit(T0)
    for i in range(60):
        r.boot(power_loss=True)
        for j in range(3):
            t = r.emit(UNSET + i)
            assert t > last, (i, t, last)
            last = t
    assert r.writes <= 61, r.writes      # one per reboot at most (no RTC to carry the exact value)


@_with_rig(rtc=True)
def test_forward_and_backward_clock_jumps(r):
    a = r.emit(T0)
    assert r.emit(T0 + 86400) == T0 + 86400            # forward: adopted as is
    b = r.emit(T0 + 86400 - 7200)                      # clock stepped back 2 h, still "valid"
    assert b == T0 + 86400 + 1                         # continues just above the last emission
    c = r.emit(T0 + 86400 - 7200 + 5)
    assert c == b + 1
    r.boot()
    d = r.emit(T0 + 86400 - 7000)                      # reboot while still behind
    assert d == c + 1
    assert r.emit(T0 + 86400 + 100) == T0 + 86400 + 100   # clock passes it: real time again
    assert a < b


def _check_random(seed):
    rnd = random.Random(seed)
    r = Rig(rtc=rnd.random() < 0.6)
    try:
        now = T0 if rnd.random() < 0.7 else UNSET
        last = 0
        valid_only = True
        backs = 0
        for step in range(150):
            ev = rnd.random()
            n = 1
            if ev < 0.25:                                # reboot (RTC lost by power cycle sometimes)
                loss = rnd.random() < 0.3
                r.boot(power_loss=loss)
                now += rnd.randint(3, 700)
                if not FakeRTC.working or loss:
                    n = 1
            elif ev < 0.35:                              # clock jump
                jump = rnd.choice([-86400, -3000, 3000, 86400 * 3])
                now += jump
                if jump < 0:
                    valid_only = False
            elif ev < 0.40:                              # unset clock after a reboot
                r.boot(power_loss=rnd.random() < 0.5)
                now = UNSET + rnd.randint(0, 50)
                valid_only = False
            else:
                now += rnd.randint(1, 4000)
            n = rnd.choice([1, 1, 1, 2])
            for k in range(n):
                t = r.emit(now)
                assert t > last, (seed, step, t, last)
                last = t
                if valid_only and now >= D.CLOCK_VALID_MIN and FakeRTC.working:
                    assert t <= now + n - 1 + 0, (seed, step, t - now)
        assert r.writes <= 150 * 2
    finally:
        r.close()


def test_random_sequences_strictly_increasing_100_seeds():
    for seed in range(100):
        _check_random(seed)


def test_random_valid_clock_never_in_the_future_100_seeds():
    for seed in range(100):
        rnd = random.Random(seed)
        r = Rig(rtc=rnd.random() < 0.5)
        try:
            now = T0
            for step in range(150):
                if rnd.random() < 0.3:
                    r.boot(power_loss=rnd.random() < 0.5)
                    now += rnd.randint(3, 700)       # a boot takes a few seconds
                else:
                    now += rnd.randint(1, 3000)
                n = rnd.choice([1, 1, 2])
                for k in range(n):
                    t = r.emit(now)
                    assert t - now <= k, (seed, step, t - now)
                if n == 2 and not FakeRTC.working:   # flash-only: a reboot right after a double needs 2 s
                    now += 2
        finally:
            r.close()


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS " + name)
        except Exception as ex:
            import traceback
            traceback.print_exc()
            print("FAIL " + name + " " + repr(ex))
            failed += 1
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
