# µReticulum stamp search: policy of the opt-in ESP32-S3 SHA accelerator. Imported lazily by
# stamps.py (only when HW_SHA is on, config key "stamps_hw_sha"), so boards that do not use
# the accelerator never load it.
import os
import sys

from . import const

# The accelerator is driven by direct register access from the native module and coordinates with
# the firmware's own users of the engine (WiFi key derivation, TLS) only
# heuristically, so a board that wants the speed-up must ask for it:
# config key "stamps_hw_sha": True (Reticulum.setup_interfaces sets HW_SHA).
# Ignored on anything but an ESP32-S3 and when the native module lacks it.
#
# Resilience (all in this file, the hardware is never trusted blindly):
#   - before first use a start-up check runs: HW_CHECK_N hashes compared against
#     software, and proof that the firmware's own engine reset (any hashlib call)
#     clears the native module's ownership sentinel (the whole coordination with
#     the firmware depends on that). Failure -> hardware off for the session.
#   - after every hardware slice the native counters are read: any digest
#     mismatch disables hardware for the session; each busy/lost event (the
#     firmware had or took the engine) starts a HW_HOLDOFF_S pause and more than
#     HW_MAX_EVENTS of them within HW_WINDOW_S disables it for the session.
#   - no hardware while WiFi is down or for HW_WIFI_SETTLE_S after it came up.
#   - every hit is verified in software regardless (native and find_hashcash).
HW_CHECK_N = 256                # hashes compared in the start-up check
HW_HOLDOFF_S = 5                # software only for this long after a busy/lost event
HW_WINDOW_S = 300               # sliding window for the event limit
HW_MAX_EVENTS = 3               # busy/lost events tolerated per window
HW_WIFI_SETTLE_S = 5            # software only for this long after WiFi (re)connected
HW_BOOT_GRACE_MS = 60000        # up before the first look, later than this after boot: assume it settled long ago


# hardware session state: reason string once disabled, None while usable
_hw_off = None
_hw_checked = False
_hw_hold_until = None           # ticks_ms until which hardware is paused
_hw_events = []                 # ticks_ms of recent busy/lost events
_hw_last = (0, 0, 0, 0)         # last seen native hwstats (n, mismatches, busy, lost)
_wifi_up_at = None              # ticks_ms when the STA interface was last seen coming up
_wifi_down_seen = False         # a down state was observed (so an up state is a real transition)


def hw_disabled():
    """Reason the hardware path was switched off for this session, else None."""
    return _hw_off


def hw_disable(reason):
    global _hw_off
    if _hw_off is None:
        _hw_off = reason
        try:
            from .log import log, LOG_WARNING
            log("Stamp SHA hardware disabled: " + reason, LOG_WARNING)
        except Exception:
            pass


def hw_reset():
    """Forget the hardware session state (tests, or after the owner re-enables)."""
    global _hw_off, _hw_checked, _hw_hold_until, _hw_events, _hw_last, _wifi_up_at, _wifi_down_seen
    _hw_off = None
    _hw_checked = False
    _hw_hold_until = None
    _hw_events = []
    _hw_last = (0, 0, 0, 0)
    _wifi_up_at = None
    _wifi_down_seen = False


def _wifi_state():
    """True/False for the STA interface; None when there is no WiFi API (host)."""
    try:
        import network
        return bool(network.WLAN(network.STA_IF).isconnected())
    except Exception:
        return None


def _hw_selfcheck(nm):
    """Start-up check. Returns None when the engine is usable, else the reason."""
    global _hw_last
    st = getattr(nm, "hwselftest", None)
    probe = getattr(nm, "hwprobe", None)
    if st is None or probe is None or not hasattr(nm, "hwstats"):
        return "native module too old for the start-up check"
    try:
        bad, badw, done = st(os.urandom(48), HW_CHECK_N)
    except Exception as e:
        return "self-test error " + repr(e)
    if done == 0:
        return "engine busy at the start-up check"   # retried later: caller treats as transient
    if bad or done < HW_CHECK_N:
        return "self-test: %d/%d hashes differ" % (bad, done) if bad else "self-test lost the engine"
    # firmware coordination: its engine reset must clear our ownership sentinel
    try:
        if not probe(0):
            return "engine busy at the coordination check"
        try:
            from hashlib import sha256 as _fw_sha     # firmware acquire + reset + release
        except ImportError:
            from .crypto import sha256 as _fw_sha
        _fw_sha(b"x").digest()
        owned = probe(1)
        probe(2)
    except Exception as e:
        try:
            probe(2)
        except Exception:
            pass
        return "coordination check error " + repr(e)
    if owned:
        return "firmware engine reset does not clear the ownership sentinel"
    _hw_last = tuple(nm.hwstats(False))
    return None


def account(nm):
    """After a hardware slice: read the counters, apply the session policy."""
    global _hw_last, _hw_hold_until
    try:
        cur = tuple(nm.hwstats(False))
    except Exception:
        return
    last = _hw_last
    _hw_last = cur
    if cur[1] > last[1]:
        hw_disable("%d digest mismatch(es) against software" % (cur[1] - last[1]))
        return
    ev = (cur[2] - last[2]) + (cur[3] - last[3])
    if ev > 0:
        now = const.ticks_ms()
        _hw_hold_until = now + HW_HOLDOFF_S * 1000
        _hw_events.append(now)
        while _hw_events and const.ticks_diff(now, _hw_events[0]) > HW_WINDOW_S * 1000:
            _hw_events.pop(0)
        if len(_hw_events) > HW_MAX_EVENTS:
            hw_disable("engine contention: %d busy/lost events in %d s" % (len(_hw_events), HW_WINDOW_S))


def usable(nm):
    """1 when this slice may use the accelerator, else 0 (never raises)."""
    global _hw_checked, _hw_hold_until, _wifi_up_at, _wifi_down_seen
    if _hw_off is not None:
        return 0
    try:
        if "ESP32S3" not in sys.implementation._machine.replace("-", "").replace(" ", ""):
            return 0
    except Exception:
        return 0
    now = const.ticks_ms()
    w = _wifi_state()
    if w is False:
        _wifi_up_at = None
        _wifi_down_seen = True
        return 0
    if w and _wifi_up_at is None:
        # Up at the first look (nobody asked before, e.g. the first search long after boot): it
        # did not just connect, so do not make that search wait out the settle time. After boot
        # or an observed down state the settle time applies.
        _wifi_up_at = now if (_wifi_down_seen or now < HW_BOOT_GRACE_MS) else now - HW_WIFI_SETTLE_S * 1000
    if _wifi_up_at is not None and const.ticks_diff(now, _wifi_up_at) < HW_WIFI_SETTLE_S * 1000:
        return 0
    if _hw_hold_until is not None:
        if const.ticks_diff(now, _hw_hold_until) < 0:
            return 0
        _hw_hold_until = None
    if not _hw_checked:
        why = _hw_selfcheck(nm)
        if why is None:
            _hw_checked = True
        elif why.startswith("engine busy"):
            _hw_hold_until = now + HW_HOLDOFF_S * 1000     # transient: try the check again later
            return 0
        else:
            hw_disable(why)
            return 0
    return 1


