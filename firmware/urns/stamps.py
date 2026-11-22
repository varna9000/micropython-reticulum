# µReticulum stamp SEARCH (Reticulum 1.6): announce-stamp generation, link-request
# "key stamps" (initiator side), path-request stamps, pacing and the hardware-SHA policy.
# Imported only by a node that searches. Validation (cost table, stamp_valid, stamp_value)
# is in stamp_check.py, which the receive path imports instead; its names are re-exported
# here for existing callers.

import gc
import os
import sys
import time
from .crypto import sha256

from . import const
from .stamp_check import (COST_FP_BITS, COST_FP, MANTISSA_BITS, STAMP_COST_CAP, _FP_CAP,
                          _MAX_TARGET, _TAB, _tab, cost_to_fp, target_from_fp, _lz_bits,
                          stamp_valid, stamp_value)

# --- key-stamp search ------------------------------------------------------
#
# Find an X25519 private scalar whose public key satisfies
#   sha256(dest_hash + initiator_ed25519_pub + x25519_pub) <= target(cost)
# Candidates are scalars k0+8i (clamped form kept), public keys come from
# x-only differential additions with batched inversion, ~60x cheaper than a
# full ladder each.

# Slices. Every search call blocks the event loop for as long as it runs, so a
# slice is sized from the MEASURED duration of the previous one (Pace) to take
# ~SLICE_TARGET_MS, whatever the CPU clock (240 vs 160 MHz differ by 1.5x). The
# constants are only the first slice's size, before any rate is known.
SLICE_TARGET_MS = 80            # interactive searches (a user is waiting)
SLICE_TARGET_BG_MS = 20         # background generation: about the announce-ingress budget, so the
                                # event loop never stalls longer than a received announce does
SLICE_NATIVE = 1000             # first native key-search slice (~55-110 us/candidate)
SLICE_PY = 32                   # one batch; pure Python is ~ms/candidate on an MCU
HASHCASH_NATIVE = 2000          # first native hashcash slice
HASHCASH_PY = 32                # first pure-Python hashcash slice
TABLE_N = 64                    # native table entries (buffer = 16 + 120*(n+1) B)

# Opt-in ESP32-S3 SHA accelerator for the native searches (default OFF). It is
# driven by direct register access from the native module and coordinates with
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
HW_SHA = False
HW_CHECK_N = 256                # hashes compared in the start-up check
HW_HOLDOFF_S = 5                # software only for this long after a busy/lost event
HW_WINDOW_S = 300               # sliding window for the event limit
HW_MAX_EVENTS = 3               # busy/lost events tolerated per window
HW_WIFI_SETTLE_S = 5            # software only for this long after WiFi (re)connected

# --- duty-cycle pacing ---------------------------------------------------------
#
# A search call blocks the event loop for a whole slice (~SLICE_TARGET_MS). The
# task that drives it then rests for a period proportional to the slice, so other
# tasks get a real share of the CPU instead of one turn per slice:
#   rest = busy * (100 - duty) / duty.
# Interactive searches (link key, path request) run flat out for the first
# GRACE_MS (short searches finish at full speed), then at DUTY_INTERACTIVE;
# background announce-stamp generation runs at DUTY_BACKGROUND from the start.
DUTY_INTERACTIVE = 80           # percent of wall time spent hashing after the grace
DUTY_BACKGROUND = 50
GRACE_MS = 1000


# Interactive searches (link key, path request, LXMF stamp) bump this counter
# for their whole life; background generation (announce stamps) yields entirely
# while it is non-zero, so a user-visible wait never competes with it.
_interactive = 0


def interactive_enter():
    global _interactive
    _interactive += 1


def interactive_exit():
    global _interactive
    if _interactive > 0:
        _interactive -= 1


def interactive_active():
    return _interactive > 0


class Duty:
    def __init__(self, duty_pct, grace_ms=0):
        self.duty = duty_pct
        self.grace = grace_ms
        self.busy = 0               # accumulated slice time, ms
        self._t = None

    def begin(self):
        self._t = const.ticks_ms()

    def rest_ms(self, busy_ms=None):
        """Sleep (ms, >= 1) to take after a slice. busy_ms defaults to the time
        since begin()."""
        if busy_ms is None:
            busy_ms = const.ticks_diff(const.ticks_ms(), self._t) if self._t is not None else 0
        if busy_ms < 0:
            busy_ms = 0
        self.busy += busy_ms
        d = self.duty
        if d >= 100 or d <= 0 or self.busy <= self.grace:
            return 1
        r = busy_ms * (100 - d) // d
        return r if r > 1 else 1


_ticks_us = getattr(time, "ticks_us", None) or (lambda: int(time.perf_counter() * 1000000))   # only the pacing needs microseconds

# hardware session state: reason string once disabled, None while usable
_hw_off = None
_hw_checked = False
_hw_hold_until = None           # ticks_ms until which hardware is paused
_hw_events = []                 # ticks_ms of recent busy/lost events
_hw_last = (0, 0, 0, 0)         # last seen native hwstats (n, mismatches, busy, lost)
_wifi_up_at = None              # ticks_ms when the STA interface was last seen coming up


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
    global _hw_off, _hw_checked, _hw_hold_until, _hw_events, _hw_last, _wifi_up_at
    _hw_off = None
    _hw_checked = False
    _hw_hold_until = None
    _hw_events = []
    _hw_last = (0, 0, 0, 0)
    _wifi_up_at = None


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


def _hw_account(nm):
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


def _hw(nm=None):
    """1 when this slice may use the accelerator, else 0 (never raises)."""
    global _hw_checked, _hw_hold_until, _wifi_up_at
    if not HW_SHA or _hw_off is not None:
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
        return 0
    if w and _wifi_up_at is None:
        _wifi_up_at = now
    if _wifi_up_at is not None and const.ticks_diff(now, _wifi_up_at) < HW_WIFI_SETTLE_S * 1000:
        return 0
    if _hw_hold_until is not None:
        if const.ticks_diff(now, _hw_hold_until) < 0:
            return 0
        _hw_hold_until = None
    if nm is None:
        nm = _native_hc() or _native()
    if nm is None:
        return 0
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


class Pace:
    """Adaptive slice size: n units per call such that a call takes ~target_ms.
    Growth is capped at 2x per call (a slice that ended early on a hit or a GC
    pause must not make the next one huge)."""

    def __init__(self, first, floor, cap, target_ms=None):
        self.first = first
        self.n = first
        self.floor = floor
        self.cap = cap
        self.target_ms = target_ms      # None: the module's SLICE_TARGET_MS (interactive)

    def reset(self):
        self.n = self.first

    def update(self, tried, dt_us):
        if tried <= 0 or dt_us <= 0:
            return
        n = tried * (self.target_ms or SLICE_TARGET_MS) * 1000 // dt_us
        if n > self.n * 2:
            n = self.n * 2
        self.n = self.floor if n < self.floor else (self.cap if n > self.cap else n)


_ks_pace = Pace(SLICE_NATIVE, 50, 20000)
_hc_pace = Pace(HASHCASH_NATIVE, 100, 200000)
_hcpy_pace = Pace(HASHCASH_PY, 1, 4096)
# Background (announce stamp) generation has its own pacing objects and a smaller slice target:
# an interactive search must not inherit slices tuned for the background, nor the other way round.
_hc_bg_pace = Pace(HASHCASH_NATIVE // 4, 25, 200000, SLICE_TARGET_BG_MS)
_hcpy_bg_pace = Pace(1, 1, 4096, SLICE_TARGET_BG_MS)

_nbuf = None
_nmod = None
_native_state = 0               # 0 unknown, 1 ready, -1 unavailable


def _native():
    """Native x25519_stamp2 module + our table buffer (allocated once)."""
    global _nbuf, _nmod, _native_state
    if _native_state:
        return _nmod if _native_state > 0 else None
    _native_state = -1
    try:
        from .crypto import ed25519 as _ed
        nm = getattr(_ed, "_native", None)
        if nm is None or not hasattr(nm, "x25519_stamp2") or not hasattr(nm, "x25519_stamp2_init"):
            return None
        for _ in range(4):      # init needs a 4-byte-aligned buffer; retry on a different block
            buf = bytearray(16 + 120 * (TABLE_N + 1))
            try:
                nm.x25519_stamp2_init(buf)
            except ValueError:
                continue
            _nbuf = buf
            _nmod = nm
            _native_state = 1
            return nm
    except Exception:
        pass
    return None


def set_table_size(n):
    """Change the native table size (frees the old buffer; re-inits lazily)."""
    global TABLE_N, _nbuf, _nmod, _native_state
    TABLE_N = int(n)
    _nbuf = None
    _nmod = None
    _native_state = 0
    _ks_pace.reset()


def have_native():
    return _native() is not None


# Largest link-request stamp cost (2^cost candidates expected) a board attempts
# when "max_link_stamp_cost" is not configured: ~50 us/candidate with the native
# key search (cost 19 is ~26 s expected against the 120 s link search budget),
# ~3 ms/candidate in pure Python (cost 12 is ~12 s).
MAX_LINK_COST_NATIVE = 19.0
MAX_LINK_COST_PY = 12.0


def default_max_link_cost():
    return MAX_LINK_COST_NATIVE if have_native() else MAX_LINK_COST_PY


def slice_size():
    """Candidates for the next find_link_key() slice."""
    return _ks_pace.n if _native() is not None else SLICE_PY


_P = 2 ** 255 - 19
_A24 = 121665


# u of 8*G (the x coordinate of the base point times 8, affine): constant, so the three
# ladders the search used to start with are two. Checked against a ladder in tests.
_U8 = 17809203070174708532389388378138548413001352690456714369618601795787323709800


def _gc_mask():
    """Same GC discipline as crypto/x25519.py (_gc_mask: boot sets it low so big-integer
    temporaries do not fragment the split heap; example nodes relax it later)."""
    try:
        from .crypto import x25519
        return x25519._gc_mask
    except Exception:
        return 7


class _Ladder:
    """RFC 7748 Montgomery ladder as a resumable state machine: run(budget_ms, t0) executes
    iterations (one bit, ~5 ms in pure Python on an ESP32) until finished or the budget is
    used up, so the event loop is never blocked for more than the budget plus one iteration.
    Result: (x2, z2) projective, in self.x2 / self.z2."""

    def __init__(self, k, u):
        self.k = k
        self.u = u
        self.t = 254
        self.x2, self.z2, self.x3, self.z3 = 1, 0, u, 1
        self.swap = 0

    def run(self, budget_ms=None, t0=None):
        P = _P
        k = self.k
        x1 = self.u
        x2, z2, x3, z3, swap = self.x2, self.z2, self.x3, self.z3, self.swap
        t = self.t
        mask = _gc_mask()
        while t >= 0:
            kt = (k >> t) & 1
            swap ^= kt
            if swap:
                x2, x3, z2, z3 = x3, x2, z3, z2
            swap = kt
            A = (x2 + z2) % P
            AA = A * A % P
            B = (x2 - z2) % P
            BB = B * B % P
            E = (AA - BB) % P
            C = (x3 + z3) % P
            D = (x3 - z3) % P
            DA = D * A % P
            CB = C * B % P
            x3 = (DA + CB) ** 2 % P
            z3 = x1 * (DA - CB) ** 2 % P
            x2 = AA * BB % P
            z2 = E * (AA + _A24 * E) % P
            if t & mask == 0:
                gc.collect()
            t -= 1
            if budget_ms is not None and t >= 0 and const.ticks_diff(const.ticks_ms(), t0) >= budget_ms:
                break
        self.x2, self.z2, self.x3, self.z3, self.swap, self.t = x2, z2, x3, z3, swap, t
        if t >= 0:
            return False
        if swap:
            self.x2, self.z2 = x3, z3
        return True


class _PySearch:
    """Resumable pure-Python incremental search (state survives across slices,
    so a slice does not repeat the start-up ladders). The two start-up ladders (k0-8
    and k0, ~1.4 s each on an ESP32) are built incrementally: run() spends at most
    init_ms of a call on them, so constructing a search and starting it never blocks
    the event loop for more than about init_ms plus one ladder iteration."""

    def __init__(self):
        b = bytearray(os.urandom(32))
        b[0] &= 248
        b[31] &= 127
        b[31] |= 64
        k0 = int.from_bytes(b, "little")
        k0 &= ~(0xFFFFFF << 3)   # bits 3..26 clear: k0+8i (i < 2^24) never carries into clamp bits
        self.k0 = k0
        u8 = _U8
        self.u8p = (u8 + 1) % _P
        self.u8m = (u8 - 1) % _P
        self.i = 0
        self.stage = 0               # 0: ladder for k0-8, 1: ladder for k0, 2: ready
        self.lad = None
        self.prev = self.cur = None

    def prepare(self, init_ms=None):
        """Advance the start-up ladders; True once ready. init_ms=None: finish them now."""
        t0 = const.ticks_ms()
        while self.stage < 2:
            if self.lad is None:
                self.lad = _Ladder(self.k0 - 8 if self.stage == 0 else self.k0, 9)
            if not self.lad.run(init_ms, t0):
                return False
            pt = (self.lad.x2, self.lad.z2)
            self.lad = None
            if self.stage == 0:
                self.prev = pt
            else:
                self.cur = pt
            self.stage += 1
            if self.stage < 2 and init_ms is not None and const.ticks_diff(const.ticks_ms(), t0) >= init_ms:
                return False
        return True

    def run(self, material, target, budget, init_ms=None):
        """One slice: (prv, pub, tried, exhausted). While the start-up ladders are not
        done it only advances them (tried 0, no key); init_ms bounds that work per call
        (None: all of it at once)."""
        if self.stage < 2 and not self.prepare(init_ms):
            return None, None, 0, False
        P = _P
        u8p = self.u8p
        u8m = self.u8m
        done = 0
        while done < budget:
            if self.i >= (1 << 24) - 64:
                return None, None, done, True       # scalar space of this start exhausted
            pts = []
            prev = self.prev
            cur = self.cur
            for _ in range(32):
                pts.append(cur)
                x2, z2 = cur
                x1, z1 = prev
                a = (x2 - z2) % P * u8p % P
                b = (x2 + z2) % P * u8m % P
                s = (a + b) % P
                t = (a - b) % P
                prev, cur = cur, (z1 * (s * s % P) % P, x1 * (t * t % P) % P)
            self.prev = prev
            self.cur = cur
            acc = 1
            pref = []
            for p in pts:
                pref.append(acc)
                acc = acc * p[1] % P
            inv = pow(acc, P - 2, P)
            us = [0] * 32
            for j in range(31, -1, -1):
                x, z = pts[j]
                us[j] = x * (inv * pref[j] % P) % P
                inv = inv * z % P
            base = self.i
            self.i += 32
            done += 32
            if (base >> 5) & _gc_mask() == 0:
                gc.collect()            # the batch's big-integer temporaries (see crypto/x25519.py)
            for j in range(32):
                pub = us[j].to_bytes(32, "little")
                if sha256(material + pub) <= target:
                    k = self.k0 + 8 * (base + j)
                    return k.to_bytes(32, "little"), pub, done - 31 + j, False
        return None, None, done, False


_py = None
_py_material = None


def find_link_key(dest_hash, sig_pub, cost, budget_candidates):
    """Search up to ~budget_candidates X25519 keys whose public key satisfies the
    link-request stamp for `cost`. Returns (prv_32, pub_32, tried) on success or
    (None, None, tried). Call repeatedly (yielding between calls) until it
    returns a key. Native: fresh random seed per call. Pure Python: one resumable
    search is kept across calls for the same (dest_hash, sig_pub), rounded up to
    32-candidate batches."""
    global _py, _py_material
    material = dest_hash + sig_pub
    target = target_from_fp(cost_to_fp(cost))
    nm = _native()
    if nm is not None:
        t0 = _ticks_us()
        hw = _hw(nm)
        if hw:
            try:
                r = nm.x25519_stamp2(os.urandom(32), material, target, budget_candidates, _nbuf, hw)
            except TypeError:           # older module without the hw argument
                r = nm.x25519_stamp2(os.urandom(32), material, target, budget_candidates, _nbuf)
            _hw_account(nm)
        else:
            r = nm.x25519_stamp2(os.urandom(32), material, target, budget_candidates, _nbuf)
        _ks_pace.update(r[2], const.ticks_diff(_ticks_us(), t0))
        return r[0], r[1], r[2]
    if _py is None or _py_material != material:
        _py = _PySearch()
        _py_material = material
    prv, pub, tried, exhausted = _py.run(material, target, budget_candidates, SLICE_TARGET_MS)
    if prv is not None or exhausted:
        _py = None
        _py_material = None
    return prv, pub, tried


def reset_search(material=None):
    """Drop the resumable pure-Python search state (after cancel/timeout). With
    `material` (dest_hash + sig_pub) only the state that belongs to that search."""
    global _py, _py_material
    if material is not None and _py_material != material:
        return
    _py = None
    _py_material = None


# The pure-Python search keeps ONE resumable state, so concurrent searches would
# evict each other every slice and none would finish. They take turns instead:
# search_turn() is True for the owner at the head of the queue (always True with
# the native search, which is stateless); search_release() hands the turn on.
_py_queue = []


def search_turn(owner):
    if _native() is not None:
        return True
    if owner not in _py_queue:
        _py_queue.append(owner)
    return _py_queue[0] is owner


def search_release(owner, material=None):
    if owner in _py_queue:
        first = _py_queue[0] is owner
        _py_queue.remove(owner)
        if first:
            reset_search(material)
    else:
        reset_search(material)


# --- hashcash / path-request stamps -----------------------------------------
#
# A stamp is any `slen` bytes with sha256(material + stamp) <= target(cost).
# Path requests (Reticulum 1.6): material = dest_hash + tag, 14-byte stamp,
# cost PR_COST; the request data is dest(16) + requester transport id(16) +
# tag(16) + 0xC1 + flag + stamp (flag 0x00 = stamped by the requester,
# 0xFF = a stamp-aware relay forwarding an unstamped request, stamp zeros).

PR_COST = 15.8                  # reference Transport.PR_COST / PR_VALUE
PR_STAMP_LEN = 14               # reference Identity.STAMPLENGTH
PR_EXT_MARKER = 0xC1
PR_LENGTH = 48                  # dest + transport id + tag
PR_STAMPED_LENGTH = 64          # + marker, flag, stamp
PR_LEGACY_LENGTH = 32           # dest + tag (pre-1.6 leaf form)

_hcmod = None
_hc_state = 0


def _native_hc():
    """Native hashcash() (newer module only), else None."""
    global _hcmod, _hc_state
    if _hc_state:
        return _hcmod if _hc_state > 0 else None
    _hc_state = -1
    try:
        from .crypto import ed25519 as _ed
        nm = _ed.hashcash_module()
        if nm is not None:
            _hcmod = nm
            _hc_state = 1
            return nm
    except Exception:
        pass
    return None


def have_hashcash():
    return _native_hc() is not None


def reset_native_probe():
    """Forget the cached native-module detection (tests swap the module)."""
    global _hcmod, _hc_state
    _hcmod = None
    _hc_state = 0
    _hc_pace.reset()
    _hcpy_pace.reset()
    _hc_bg_pace.reset()
    _hcpy_bg_pace.reset()


def hashcash_slice_size(background=False):
    if _native_hc() is not None:
        return (_hc_bg_pace if background else _hc_pace).n
    return (_hcpy_bg_pace if background else _hcpy_pace).n


def find_hashcash(material, target, slen, budget=None, background=False):
    """Try up to `budget` stamps (default: sized from the previous slice's
    duration). Returns (stamp or None, tried). Stateless: call repeatedly,
    yielding between calls, until a stamp comes back. A found stamp is
    always re-verified in software (the native hit path does it too).
    background=True (announce-stamp generation) sizes slices for
    SLICE_TARGET_BG_MS with its own pacing state instead of SLICE_TARGET_MS."""
    nm = _native_hc()
    t0 = _ticks_us()
    hc_pace = _hc_bg_pace if background else _hc_pace
    hcpy_pace = _hcpy_bg_pace if background else _hcpy_pace
    if nm is not None:
        if budget is None:
            budget = hc_pace.n
        hw = _hw(nm)
        if hw:
            stamp, tried = nm.hashcash(material, target, slen, budget, os.urandom(32), hw)
            _hw_account(nm)
        else:
            stamp, tried = nm.hashcash(material, target, slen, budget, os.urandom(32))
        hc_pace.update(tried, const.ticks_diff(_ticks_us(), t0))
    else:
        if budget is None:
            budget = hcpy_pace.n
        base = os.urandom(slen - 4)
        stamp = None
        tried = 0
        for i in range(budget):
            c = i.to_bytes(4, "little") + base
            tried += 1
            if sha256(material + c) <= target:
                stamp = c
                break
        hcpy_pace.update(tried, const.ticks_diff(_ticks_us(), t0))
    if stamp is not None and sha256(material + stamp) > target:
        return None, tried
    return stamp, tried


def pr_material(dest_hash, tag):
    return dest_hash + tag


def validate_pr_stamp(stamp, dest_hash, tag, cost=PR_COST):
    """Reference Transport.validate_pr_stamp."""
    if not stamp or len(stamp) != PR_STAMP_LEN:
        return False
    return stamp_valid(stamp, cost, pr_material(dest_hash, tag))


def find_pr_stamp(dest_hash, tag, cost=PR_COST, budget=None):
    """One slice of the path-request stamp search -> (stamp or None, tried)."""
    return find_hashcash(pr_material(dest_hash, tag), target_from_fp(cost_to_fp(cost)),
                         PR_STAMP_LEN, budget)

