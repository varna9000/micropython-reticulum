# µReticulum announce stamps (Reticulum 1.6): our own destinations' stamps.
#
# A 1.6.x hub that enforces stamps drops announces whose app_data does not end in
#   0xC1 | flags 0x00 | stamp(14) | link-cost fraction | link-cost integer
# with sha256(dest_hash + public_key + name_hash + stamp) <= target(cost). This
# module stores one stamp per destination, finds one in the background, and builds
# the 18-byte extension that Destination.announce() appends inside the signed data.
#
# Storage (same layout as the reference, files are interchangeable):
#   <storage>/stamps/<dest_hex>.dstamp = msgpack {"v": 1, "stamp": bytes(14),
#                                                 "value": float, "generated": t}
# Loaded stamps are re-checked against the destination's own material; the stored
# "value" is never trusted. Generation never delays announces: until a stamp
# exists they go out unstamped exactly as before.
#
# Config (Reticulum.setup_interfaces): stamp_announces (default: True when the
# native hashcash exists), announce_stamp_value (25.0), announce_stamp_allow_low
# (False: a value below 23.5 is clamped, True: private networks), announce_stamp_duty
# (50 % of wall time spent hashing), announce_stamp_required (23.5: the cost an
# INBOUND announce's stamp must reach to be accepted, identity._split_ext; the
# receive-side twin of announce_stamp_allow_low -- a private network sets both).
# A stored stamp below announce_stamp_value is used until a new one at the
# configured value has been generated in the background and replaces it.

import os
import time
from . import const
from .log import log, LOG_INFO, LOG_NOTICE, LOG_WARNING, LOG_ERROR, LOG_VERBOSE

EXT_MARKER = 0xC1
EXT_LEN = 18
STAMP_LEN = 14
VALUE_DEFAULT = 25.0            # reference Transport.DESTINATION_VALUE
MIN_COST = 23.5                 # reference Transport.DESTINATION_COST (hub default)
LINK_COST_CAP = 40
FILE_VERSION = 1
MAX_ENTRIES = 8                 # RAM bound on cached stamps / generation tasks

ENABLED = False                 # background generation on/off (loading is always on)
VALUE = VALUE_DEFAULT           # target value of generated stamps
MIN = MIN_COST                  # a stored stamp must reach at least this
DUTY = 50
storage = None                  # base dir; defaults to Identity.storagepath

_cache = {}                     # dest_hash -> stamp bytes
_tasks = {}                     # dest_hash -> asyncio task
_pending = {}                   # dest_hash -> destination, waiting for an event loop
_logged = {}                    # dest_hash -> True once the material was logged
_at_value = {}                  # dest_hash -> VALUE its cached stamp was verified to reach
_unstamped = {}                 # dest_hash -> destination whose last announce went out without a stamp
_upgrading = {}                 # dest_hash -> True once the "below configured value" line was logged


def configure(enabled=None, value=None, allow_low=False, duty=None, storage_dir=None):
    global ENABLED, VALUE, MIN, DUTY, storage
    if enabled is not None:
        ENABLED = bool(enabled)
    v = VALUE_DEFAULT if value is None else float(value)
    if v < MIN_COST and not allow_low:
        log("announce_stamp_value " + str(v) + " is below the hub default "
            + str(MIN_COST) + ", using " + str(MIN_COST), LOG_WARNING)
        v = MIN_COST
    VALUE = v
    MIN = v if v < MIN_COST else MIN_COST
    if duty is not None:
        DUTY = int(duty)
    if storage_dir is not None:
        storage = storage_dir


def reset():
    """Forget all state (tests)."""
    global ENABLED, VALUE, MIN, DUTY, storage
    for t in list(_tasks.values()):
        try:
            t.cancel()
        except Exception:
            pass
    global _current
    _current = None
    del _queue[:]
    _cache.clear()
    _tasks.clear()
    _pending.clear()
    _logged.clear()
    _at_value.clear()
    _upgrading.clear()
    _unstamped.clear()
    ENABLED = False
    VALUE = VALUE_DEFAULT
    MIN = MIN_COST
    DUTY = 50
    storage = None


def material(dest):
    return dest.hash + dest.identity.get_public_key() + dest.name_hash


def _dir():
    if storage is not None:
        return storage + "/stamps"
    from .destination import Destination
    return Destination.stamps_dir()


def _path(dest_hash):
    return _dir() + "/" + dest_hash.hex() + ".dstamp"


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _remember(dest_hash, stamp):
    if dest_hash not in _cache and len(_cache) >= MAX_ENTRIES:
        for k in _cache:
            del _cache[k]
            break
    _cache[dest_hash] = stamp


def load(dest):
    """Stamp from storage if the file exists and validates, else None."""
    path = _path(dest.hash)
    if not _exists(path):
        return None
    try:
        from . import umsgpack
        with open(path, "rb") as f:
            entry = umsgpack.unpackb(f.read())
        if entry.get("v") != FILE_VERSION:
            return None
        stamp = entry["stamp"]
        if not isinstance(stamp, bytes) or len(stamp) != STAMP_LEN:
            log("Stored stamp for " + dest.hexhash[:8] + " has an invalid format, ignored", LOG_WARNING)
            return None
        from . import stamp_check
        if not stamp_check.stamp_valid(stamp, MIN, material(dest)):
            log("Stored stamp for " + dest.hexhash[:8] + " is below cost " + str(MIN)
                + " or does not match this destination, ignored", LOG_WARNING)
            return None
        return stamp
    except Exception as e:
        log("Ignoring unreadable stamp file for " + dest.hexhash[:8] + ": " + repr(e), LOG_WARNING)
        return None


def store(dest_hash, stamp, value):
    """Persist a stamp (atomic rename). True on success."""
    try:
        from . import umsgpack
        d = _dir()
        try:
            os.stat(d)
        except OSError:
            os.mkdir(d)
        path = _path(dest_hash)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(umsgpack.packb({"v": FILE_VERSION, "stamp": stamp, "value": value,
                                    "generated": time.time()}))
        try:
            os.remove(path)
        except OSError:
            pass
        os.rename(tmp, path)
        return True
    except Exception as e:
        log("Could not persist stamp: " + repr(e), LOG_WARNING)
        return False


def get_stamp(dest):
    s = _cache.get(dest.hash)
    if s is None:
        s = load(dest)
        if s is not None:
            _remember(dest.hash, s)
    return s


def extension(dest, stamp):
    """The 18 extension bytes: marker, flags, stamp, cost fraction, cost integer.
    Link-request cost: dest.link_cost (default 0 = no stamp needed from
    initiators; the reference initiator skips the search when the announced cost
    is falsy)."""
    c = getattr(dest, "link_cost", None)
    if not c:
        c = 0
    if c > LINK_COST_CAP:
        c = LINK_COST_CAP
    fp = int(c * 256 + 0.5)
    return bytes((EXT_MARKER, 0x00)) + stamp + bytes((fp & 0xFF, (fp >> 8) & 0x7F))


def for_announce(dest):
    """Extension bytes to append to app_data, or None (no stamp yet). Also makes
    sure a missing stamp gets generated."""
    s = get_stamp(dest)
    request(dest)               # no stamp: generate; a stamp below VALUE: upgrade in the background
    if s is None:
        if len(_unstamped) < MAX_ENTRIES or dest.hash in _unstamped:
            _unstamped[dest.hash] = dest   # remembered: one announce follows when the stamp is ready
        return None
    _unstamped.pop(dest.hash, None)
    return extension(dest, s)


def request(dest):
    """Log the material once and, when enabled, start background generation for
    a destination that has no stamp."""
    h = dest.hash
    if h in _tasks or h in _pending:
        return
    s = get_stamp(dest)
    if s is not None:
        if _at_value.get(h) == VALUE:
            return
        from . import stamp_check
        if stamp_check.stamp_valid(s, VALUE, material(dest)):
            _at_value[h] = VALUE
            return
        # Kept and used (better than none) while a stamp at the configured value
        # is generated; the new one replaces it (store + _remember).
        if h not in _upgrading:
            _upgrading[h] = True
            log("Stored announce stamp for " + dest.hexhash[:8] + " is below announce_stamp_value "
                + str(VALUE) + ": still used, " + ("generating a new one" if ENABLED else
                "stamp generation is off so it stays"), LOG_NOTICE)
        if not ENABLED or len(_tasks) + len(_pending) >= MAX_ENTRIES:
            return
        _pending[h] = dest
        kick()
        return
    if h not in _logged:
        _logged[h] = True
        log("No announce stamp for " + dest.hexhash + "; stamp material "
            + material(dest).hex(), LOG_INFO)
    if not ENABLED or len(_tasks) + len(_pending) >= MAX_ENTRIES:
        return
    _pending[h] = dest
    kick()


def kick():
    """Create generation tasks for pending destinations (needs an event loop;
    Reticulum.run() calls this again once it is running)."""
    for h in list(_pending):
        dest = _pending[h]
        coro = _generate(dest)
        try:
            import uasyncio as asyncio
            _tasks[h] = asyncio.create_task(coro)
            del _pending[h]
        except Exception:
            coro.close()
            return


_queue = []             # dest hashes waiting for their turn (FIFO)
_current = None         # dest hash being generated right now (one at a time)


async def _generate(dest):
    global _current
    import uasyncio as asyncio
    from . import stamps
    h = dest.hash
    _queue.append(h)
    try:
        # One generation at a time: the rest wait their turn.
        while _current is not None or _queue[0] != h:
            await asyncio.sleep_ms(250)
        _current = h
        mat = material(dest)
        target = stamps.target_from_fp(stamps.cost_to_fp(VALUE))
        duty = stamps.Duty(DUTY)
        t0 = const.ticks_ms()
        tried_total = 0
        log("Announce stamp generation started for " + dest.hexhash[:8] + " (value "
            + str(VALUE) + ", duty " + str(DUTY) + "%)", LOG_NOTICE)
        while True:
            if stamps.interactive_active():
                # an interactive search owns the CPU: background work yields entirely
                await asyncio.sleep_ms(100)
                continue
            duty.begin()
            stamp, tried = stamps.find_hashcash(mat, target, STAMP_LEN, background=True)
            tried_total += tried
            if stamp is not None:
                break
            await asyncio.sleep_ms(duty.rest_ms())
        ms = const.ticks_diff(const.ticks_ms(), t0)
        value = stamps.stamp_value(mat, stamp)
        store(h, stamp, value)
        _remember(h, stamp)
        log("Announce stamp for " + dest.hexhash[:8] + " ready: value " + str(value)
            + " in " + str(ms // 1000) + " s (" + str(tried_total) + " hashes, "
            + str(tried_total * 1000 // max(1, ms)) + " H/s); the next announce carries it", LOG_NOTICE)
        # The last announce went out without a stamp, so an enforcing hub dropped
        # it and (with hourly re-announce) the node would stay invisible for up to
        # an hour: send exactly ONE announce now, through the normal announce path.
        # Popped first, so this can never repeat; an announce that already carried
        # a stamp (an upgrade of a weaker stamp) is not re-sent.
        if _unstamped.pop(h, None) is not None:
            try:
                dest.announce()
                log("Announce re-sent with the new stamp for " + dest.hexhash[:8], LOG_NOTICE)
            except Exception as e:
                log("Stamp-ready announce failed: " + repr(e), LOG_ERROR)
    except Exception as e:
        log("Announce stamp generation failed: " + repr(e), LOG_ERROR)
    finally:
        if _current == h:
            _current = None
        if h in _queue:
            _queue.remove(h)
        _tasks.pop(h, None)
