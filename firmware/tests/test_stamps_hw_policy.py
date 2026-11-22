# Resilience policy of the opt-in ESP32-S3 SHA path (host-side, fake native module). Run:
#   python3 firmware/tests/test_stamps_hw_policy.py
# Covers: start-up check (hw vs sw hashes, firmware-reset-clears-sentinel proof), session disable on
# mismatch / too many busy+lost events, hold-off after an event, WiFi gating, hits re-verified.

import importlib
import sys
import types

import harness  # noqa: F401  (puts firmware/ on sys.path)

stamps = importlib.import_module("urns.stamps")
stamp_hw = importlib.import_module("urns.stamp_hw")
ed = importlib.import_module("urns.crypto.ed25519")

CLK = [100000]


class Fake:
    def __init__(self, selftest=None, owned_after_reset=0, claim=1):
        self.selftest = selftest            # None -> (0, 0, n)
        self.owned_after_reset = owned_after_reset
        self.claim = claim
        self.stats = [0, 0, 0, 0]
        self.calls = []
        self.probes = []

    def hwselftest(self, material, n):
        self.calls.append(("selftest", n))
        return self.selftest if self.selftest is not None else (0, 0, n)

    def hwprobe(self, op):
        self.probes.append(op)
        if op == 0:
            return self.claim
        if op == 1:
            return self.owned_after_reset
        return 0

    def hwstats(self, reset):
        return tuple(self.stats)

    def hashcash(self, material, target, slen, budget, seed, *hw):
        self.calls.append(("hc", hw))
        return None, budget

    def x25519_stamp2_init(self, buf):
        return 1

    def x25519_stamp2(self, seed, material, target, budget, buf, *hw):
        self.calls.append(("ks", hw))
        return None, None, budget


class Env:
    def __enter__(self):
        self.old = (getattr(ed, "_native", None), stamps.HW_SHA, stamps.const.ticks_ms,
                    getattr(sys.implementation, "_machine", None), sys.modules.get("network"))
        self.fake = None
        stamps.HW_SHA = True
        stamps.const.ticks_ms = lambda: CLK[0]
        sys.implementation._machine = "Generic ESP32S3 module with ESP32S3"
        sys.modules.pop("network", None)
        stamp_hw.hw_reset()
        stamps._hw_mod = None
        stamps.reset_native_probe()
        stamps.set_table_size(4)
        return self

    def use(self, fake):
        self.fake = fake
        ed._native = fake
        stamps.reset_native_probe()
        stamp_hw.hw_reset()
        return fake

    def __exit__(self, *a):
        ed._native, stamps.HW_SHA, stamps.const.ticks_ms, mach, net = self.old
        if mach is None:
            del sys.implementation._machine
        else:
            sys.implementation._machine = mach
        if net is None:
            sys.modules.pop("network", None)
        else:
            sys.modules["network"] = net
        stamp_hw.hw_reset()
        stamps._hw_mod = None
        stamps.reset_native_probe()
        stamps.set_table_size(64)


def _slice():
    return stamps.find_hashcash(b"m" * 32, b"\x00" * 32, 14, 10)


def _fake_network(state):
    n = types.ModuleType("network")
    n.STA_IF = 0

    class W:
        def __init__(self, i):
            pass

        def isconnected(self):
            return state[0]
    n.WLAN = W
    sys.modules["network"] = n


def test_healthy_start_checks_once_then_uses_hardware():
    with Env() as e:
        f = e.use(Fake())
        assert stamps._hw() == 1 and stamps._hw() == 1
        assert [c for c in f.calls if c[0] == "selftest"] == [("selftest", stamp_hw.HW_CHECK_N)]
        assert f.probes == [0, 1, 2]                      # claim, owned?, release
        assert stamp_hw.hw_disabled() is None
        _slice()
        assert f.calls[-1] == ("hc", (1,))


def test_selftest_mismatch_disables_for_the_session():
    with Env() as e:
        f = e.use(Fake(selftest=(3, 5, stamp_hw.HW_CHECK_N)))
        assert stamps._hw() == 0
        assert "differ" in stamp_hw.hw_disabled()
        _slice()
        assert f.calls[-1] == ("hc", ())                  # software call, no hw argument
        assert [c for c in f.calls if c[0] == "selftest"] == [("selftest", stamp_hw.HW_CHECK_N)]   # not retried


def test_old_native_module_without_check_disables():
    class Old:
        def hashcash(self, *a):
            return None, a[3]
        def x25519_stamp2_init(self, b):
            return 1
        def x25519_stamp2(self, *a):
            return None, None, a[3]
    with Env() as e:
        e.use(Old())
        assert stamps._hw() == 0 and "too old" in stamp_hw.hw_disabled()


def test_sentinel_surviving_firmware_reset_disables():
    with Env() as e:
        e.use(Fake(owned_after_reset=1))
        assert stamps._hw() == 0 and "sentinel" in stamp_hw.hw_disabled()


def test_engine_busy_at_check_is_retried_later_not_disabled():
    with Env() as e:
        f = e.use(Fake(selftest=(0, 0, 0)))
        assert stamps._hw() == 0 and stamp_hw.hw_disabled() is None
        assert stamps._hw() == 0                           # still in the hold-off
        f.selftest = None
        CLK[0] += stamp_hw.HW_HOLDOFF_S * 1000 + 1
        assert stamps._hw() == 1


def test_mismatch_after_a_slice_disables():
    with Env() as e:
        f = e.use(Fake())
        _slice()
        f.stats[1] = 1
        _slice()
        assert "mismatch" in stamp_hw.hw_disabled()
        assert stamps._hw() == 0


def test_busy_or_lost_event_holds_off_then_resumes():
    with Env() as e:
        f = e.use(Fake())
        _slice()
        f.stats[2] += 1                                   # found busy
        _slice()
        assert stamp_hw.hw_disabled() is None
        assert stamps._hw() == 0                          # hold-off
        CLK[0] += stamp_hw.HW_HOLDOFF_S * 1000 - 1
        assert stamps._hw() == 0
        CLK[0] += 2
        assert stamps._hw() == 1


def test_too_many_events_in_window_disables_but_old_events_expire():
    with Env() as e:
        f = e.use(Fake())
        _slice()
        for _ in range(stamp_hw.HW_MAX_EVENTS):
            f.stats[3] += 1                               # taken from us
            CLK[0] += stamp_hw.HW_HOLDOFF_S * 1000 + 1
            _slice()
        assert stamp_hw.hw_disabled() is None               # exactly the tolerated number
        CLK[0] += stamp_hw.HW_WINDOW_S * 1000               # old events leave the window
        f.stats[3] += 1
        _slice()
        assert stamp_hw.hw_disabled() is None
        for _ in range(stamp_hw.HW_MAX_EVENTS):
            f.stats[2] += 1
            CLK[0] += stamp_hw.HW_HOLDOFF_S * 1000 + 1
            _slice()
        assert "contention" in stamp_hw.hw_disabled()


def test_wifi_gating_and_settle_time():
    with Env() as e:
        up = [False]
        _fake_network(up)
        e.use(Fake())
        assert stamps._hw() == 0                          # WiFi down
        up[0] = True
        assert stamps._hw() == 0                          # just came up
        CLK[0] += stamp_hw.HW_WIFI_SETTLE_S * 1000 - 1
        assert stamps._hw() == 0
        CLK[0] += 2
        assert stamps._hw() == 1
        up[0] = False
        assert stamps._hw() == 0
        up[0] = True
        assert stamps._hw() == 0                          # reconnect restarts the settle time
        CLK[0] += stamp_hw.HW_WIFI_SETTLE_S * 1000 + 1
        assert stamps._hw() == 1


def test_first_look_with_wifi_long_up_does_not_wait_out_the_settle_time():
    # Found on the board: the first stamp search after boot, an hour into a connected session,
    # ran in software for its first 5 s because "WiFi came up" was taken at the first look.
    with Env() as e:
        up = [True]
        _fake_network(up)
        e.use(Fake())
        CLK[0] = 10 * 60 * 1000                           # 10 minutes after boot
        assert stamps._hw() == 1
        # a first look within the boot grace still waits (the SHA clock is held on until WiFi joined)
        stamp_hw.hw_reset()
        stamps._hw_mod = stamp_hw
        CLK[0] = 20 * 1000
        assert stamps._hw() == 0
        CLK[0] += stamp_hw.HW_WIFI_SETTLE_S * 1000 + 1
        assert stamps._hw() == 1


def _fake_network2(sta, ap=None):
    """sta/ap: [active, connected] lists the test mutates."""
    ap = ap if ap is not None else [False, False]
    n = types.ModuleType("network")
    n.STA_IF = 0
    n.AP_IF = 1

    class W:
        def __init__(self, i):
            self.s = sta if i == 0 else ap

        def active(self):
            return self.s[0]

        def isconnected(self):
            return self.s[0] and self.s[1]
    n.WLAN = W
    sys.modules["network"] = n
    return ap


def test_station_never_started_uses_hardware():
    # LoRa-only board (T-Deck): no supplicant, nothing else drives the engine but hashlib,
    # which the sentinel handles. Measured on a T-Deck Pro: 195k H/s vs 34k software.
    with Env() as e:
        _fake_network2([False, False])
        e.use(Fake())
        CLK[0] = 10 * 60 * 1000
        assert stamps._hw() == 1


def test_station_switched_on_mid_search_falls_back_until_joined_and_settled():
    with Env() as e:
        sta = [False, False]
        _fake_network2(sta)
        e.use(Fake())
        CLK[0] = 10 * 60 * 1000
        assert stamps._hw() == 1
        sta[0] = True                                     # app turned WiFi on: joining
        assert stamps._hw() == 0
        sta[1] = True                                     # joined: settle time applies
        assert stamps._hw() == 0
        CLK[0] += stamp_hw.HW_WIFI_SETTLE_S * 1000 + 1
        assert stamps._hw() == 1
        sta[0] = sta[1] = False                           # switched off again
        assert stamps._hw() == 1


def test_station_off_then_joined_later_still_waits_the_settle_time():
    # Off is a "down" state: a join seen afterwards is fresh, even long after boot.
    with Env() as e:
        sta = [False, False]
        _fake_network2(sta)
        e.use(Fake())
        CLK[0] = 10 * 60 * 1000
        assert stamps._hw() == 1
        sta[0] = sta[1] = True                            # joined between two looks
        assert stamps._hw() == 0
        CLK[0] += stamp_hw.HW_WIFI_SETTLE_S * 1000 + 1
        assert stamps._hw() == 1


def test_access_point_active_keeps_software():
    with Env() as e:
        ap = _fake_network2([False, False], [True, False])
        e.use(Fake())
        CLK[0] = 10 * 60 * 1000
        assert stamps._hw() == 0                          # AP does WPA for its clients
        ap[0] = False
        assert stamps._hw() == 1


def test_boot_held_sha_clock_is_released_before_the_selftest():
    # Measured on a T-Deck Pro: the SHA clock is on from boot until the first firmware SHA
    # use. With no hashlib call yet (WiFi off, early search) the native busy check saw the
    # engine as held forever. The start-up check makes one firmware hash first.
    with Env() as e:
        _fake_network2([False, False])
        released = [False]

        class BootHeld(Fake):
            def hwselftest(self, material, n):
                self.calls.append(("selftest", n))
                return (0, 0, n) if released[0] else (0, 0, 0)    # done=0: engine busy

        f = e.use(BootHeld())
        real = stamp_hw._fw_sha256
        stamp_hw._fw_sha256 = lambda data: released.__setitem__(0, True) or real(data)
        try:
            CLK[0] = 10 * 60 * 1000
            assert stamps._hw() == 1, f.calls
        finally:
            stamp_hw._fw_sha256 = real


def test_off_by_default_never_touches_the_module():
    with Env() as e:
        f = e.use(Fake())
        stamps.HW_SHA = False
        _slice()
        assert f.calls == [("hc", ())] and f.probes == []


def _run():
    import traceback
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
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


def test_failed_lazy_import_never_raises_and_is_not_retried():
    # stamp_hw is imported on first use; on a board without the heap for it the
    # import fails: the search must carry on in software, and the failure must
    # be remembered so the import is not attempted on every slice.
    with Env() as e:
        f = e.use(Fake())
        real = sys.modules["urns.stamp_hw"]
        pkg = sys.modules["urns"]
        delattr(pkg, "stamp_hw")                         # `from . import` checks the package attribute first
        sys.modules["urns.stamp_hw"] = None              # ... then the module table -> ImportError
        try:
            for _ in range(3):
                assert _slice() == (None, 10)            # software slice, no exception
                assert stamps._hw(f) == 0
            assert stamps._hw_mod is False
            sys.modules["urns.stamp_hw"] = real          # importable again: still not retried
            assert stamps._hw(f) == 0 and stamps._hw_mod is False
        finally:
            sys.modules["urns.stamp_hw"] = real
            pkg.stamp_hw = real
        assert [c for c in f.calls if c[0] == "hc"] == [("hc", ())] * 3
        assert [c for c in f.calls if c[0] == "selftest"] == []


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
