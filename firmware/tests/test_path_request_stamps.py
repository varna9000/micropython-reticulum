# Reticulum 1.6 stamped path requests (host-side). Run:
#   python3 firmware/tests/test_path_request_stamps.py
#
# The VECTORS were produced by reference RNS 1.6.1: RNS.Stamps.generate_stamp(
# Transport.pr_stamp_material(dest, tag), stamp_cost=Transport.PR_VALUE (15.8),
# expand_rounds=0, size=14), checked with Transport.validate_pr_stamp, and the
# request data laid out as the reference emits it (dest | tid | tag | 0xC1 0x00
# | stamp = 64 bytes). WEAK is a stamp valid at cost 6 but not at 15.8.

import asyncio
import importlib
import time

import harness
from harness import const, packet, Transport, MockInterface, reset_transport, set_identity

stamps = importlib.import_module("urns.stamps")
stamp_hw = importlib.import_module("urns.stamp_hw")

# (dest, tid, tag, stamp, reference stamp value, 64-byte request data)
VECTORS = [
    ('697e03670669f4df8c62dc7402c6fc23', 'de57826309ebd2a06097e6f2f25d7b5f', '9ccdea48c325e9c0dfc005c17c3a4de6',
     'e34c4346ba56282b1eb9ea8091a4', 17.4140625,
     '697e03670669f4df8c62dc7402c6fc23de57826309ebd2a06097e6f2f25d7b5f9ccdea48c325e9c0dfc005c17c3a4de6c100e34c4346ba56282b1eb9ea8091a4'),
    ('b96f45bca12e702cf1da0e113b1bd11c', 'a70d41727df61f21ce0ec81cca51c58f', '75e8dafb2eb89a1da9dc23ae727a2b4a',
     '57481413bf21354a64c5eaf55a5b', 15.94921875,
     'b96f45bca12e702cf1da0e113b1bd11ca70d41727df61f21ce0ec81cca51c58f75e8dafb2eb89a1da9dc23ae727a2b4ac10057481413bf21354a64c5eaf55a5b'),
    ('1f2b869327ecb75da1ddd45cd412b49c', 'e39994d056999d79ff5a35b02cf2af94', '94ed022ea17a947101df44b9a9f6e195',
     'fd3a697b4a1fb89b1761bc2dfb5f', 16.5859375,
     '1f2b869327ecb75da1ddd45cd412b49ce39994d056999d79ff5a35b02cf2af9494ed022ea17a947101df44b9a9f6e195c100fd3a697b4a1fb89b1761bc2dfb5f'),
]
WEAK = ('481ba4019c9d2710c3386537382592c0', 'bdddc41f7b70075bd5d5ecb2bb6d3748', 'a6ef4957eb92a246fcdd2dbfce4e')

DEST = b"\xD1" * 16
PR_DEST_HASH = b"\x5a" * 16


class _PRDest:
    """Stands in for the PLAIN rnstransport.path.request destination."""
    hash = PR_DEST_HASH
    type = 0x02
    direction = 0x12
    identity = None

    def encrypt(self, data, *a, **k):
        return data


def _setup(n_ifaces=1, transport=False):
    reset_transport()
    Transport.transport_enabled = transport
    Transport._pr_stamping = {}
    Transport._path_waiters = {}
    Transport._path_request_dest = _PRDest()
    Transport.stamp_path_requests = True
    Transport.pr_stamp_timeout = 30
    ifs = [MockInterface("if%d" % i) for i in range(n_ifaces)]
    Transport.interfaces = ifs
    return ifs


def _payload(raw):
    """Data portion of an HDR_1 packet (19-byte header: flags hops dest ctx)."""
    assert raw[0] & 0x40 == 0, "path requests are HDR_1"
    return raw[19:]


def _sent_requests(iface):
    out = []
    for raw in iface.sent:
        if raw[2:18] == PR_DEST_HASH:
            out.append(_payload(raw))
    return out


# --------------------------------------------------------------------------
def test_reference_vectors_stamp_valid():
    for d, t, g, st, val, data in VECTORS:
        dest, tag, stamp = bytes.fromhex(d), bytes.fromhex(g), bytes.fromhex(st)
        assert len(bytes.fromhex(data)) == stamps.PR_STAMPED_LENGTH == 64
        assert stamps.validate_pr_stamp(stamp, dest, tag)
        assert stamps.validate_pr_stamp(stamp, dest, tag, stamps.PR_COST)
        # exact reference value of the stamp (same fixed-point machinery)
        assert stamps.stamp_value(stamps.pr_material(dest, tag), stamp) == val
        # a stamp is bound to its dest and tag
        assert not stamps.validate_pr_stamp(stamp, dest[::-1], tag)
        assert not stamps.validate_pr_stamp(stamp, dest, tag[::-1])
        assert not stamps.validate_pr_stamp(stamp[:-1], dest, tag)          # wrong length
        assert not stamps.validate_pr_stamp(b"", dest, tag)


def test_reference_weak_stamp_rejected():
    dest, tag, stamp = (bytes.fromhex(x) for x in WEAK)
    assert stamps.stamp_valid(stamp, 6.0, stamps.pr_material(dest, tag))
    assert not stamps.validate_pr_stamp(stamp, dest, tag)


def test_pr_constants_match_reference():
    assert stamps.PR_COST == 15.8 and stamps.PR_STAMP_LEN == 14
    assert (stamps.PR_STAMPED_LENGTH, stamps.PR_LENGTH, stamps.PR_LEGACY_LENGTH) == (64, 48, 32)


def test_find_pr_stamp_python_fallback_produces_valid_stamps():
    stamps.reset_native_probe()
    d, t = b"\x01" * 16, b"\x02" * 16
    total = 0
    for _ in range(100000):
        stamp, tried = stamps.find_pr_stamp(d, t)
        total += tried
        if stamp:
            break
    assert stamp is not None and len(stamp) == 14
    assert stamps.validate_pr_stamp(stamp, d, t)


def test_find_hashcash_uses_native_when_present_and_rechecks_hits():
    calls = []

    class Fake:
        def hashcash(self, material, target, slen, budget, seed, *hw):
            calls.append((len(material), len(target), slen, budget, len(seed), hw))
            return (b"\x00" * slen, budget)         # lies: not a valid stamp

    ed = importlib.import_module("urns.crypto.ed25519")
    old = getattr(ed, "_native", None)
    ed._native = Fake()
    stamps.reset_native_probe()
    try:
        r = stamps.find_hashcash(b"m" * 32, b"\x00" * 32, 14, 777)
        assert r == (None, 777)                      # an invalid native hit is never returned
        assert calls == [(32, 32, 14, 777, 32, ())]
        stamps.HW_SHA = False
        assert stamps._hw() == 0
    finally:
        ed._native = old
        stamps.reset_native_probe()


# --------------------------------------------------------------------------
def test_parse_path_request_three_formats():
    dest, tid, tag = b"\x11" * 16, b"\x22" * 16, b"\x33" * 16
    ext = bytes([0xC1, 0x00]) + b"\x44" * 14
    P = Transport._parse_path_request
    assert P(dest + tid + tag + ext) == (dest, tid, tag, ext)            # 64 B stamped
    assert P(dest + tid + tag) == (dest, tid, tag, None)                 # 48 B unstamped
    assert P(dest + tag) == (dest, None, tag, None)                      # 32 B legacy
    assert P(dest + tid + tag + b"\x55" * 5) == (dest, tid, tag, None)   # odd size: tag truncated, ext ignored
    assert P(dest + b"\x01") == (dest, None, b"\x01", None)
    assert P(dest) is None and P(b"") is None                            # tagless
    for d, t, g, st, val, data in VECTORS:
        r = P(bytes.fromhex(data))
        assert r == (bytes.fromhex(d), bytes.fromhex(t), bytes.fromhex(g),
                     bytes([0xC1, 0x00]) + bytes.fromhex(st))


def test_handler_parses_64_byte_request_and_dedups_by_dest_and_tag():
    ifs = _setup(1, transport=True)
    seen = []
    real = Transport._answer_path_request
    Transport._answer_path_request = staticmethod(lambda *a: seen.append(a))
    try:
        class P:
            receiving_interface = ifs[0]
        d, t, g, st, val, data = VECTORS[0]
        raw = bytes.fromhex(data)
        Transport.path_request_handler(raw, P())
        Transport.path_request_handler(raw, P())                 # duplicate (dest, tag)
        assert len(seen) == 1
        dest, iface, tid, tag, ext = seen[0]
        assert dest == bytes.fromhex(d) and tid == bytes.fromhex(t) and tag == bytes.fromhex(g)
        assert ext == bytes([0xC1, 0x00]) + bytes.fromhex(st)
        Transport.path_request_handler(raw + b"\x00", P())       # > 64 B: dropped
        Transport.path_request_handler(raw[:16], P())            # tagless: dropped
        assert len(seen) == 1
    finally:
        Transport._answer_path_request = real


# --------------------------------------------------------------------------
def test_request_path_inline_sends_valid_stamped_64_byte_request():
    ifs = _setup()
    stamps.reset_native_probe()
    Transport.request_path(DEST)                    # no loop running: inline
    reqs = _sent_requests(ifs[0])
    assert len(reqs) == 1
    r = reqs[0]
    assert len(r) == 64
    dest, tid, tag, ext = Transport._parse_path_request(r)
    assert dest == DEST
    assert tid == Transport.identity.hash                    # leaf nodes include their id too
    assert ext[0] == 0xC1 and ext[1] == 0x00 and len(ext) == 16
    assert stamps.validate_pr_stamp(ext[2:], dest, tag)
    assert Transport._pr_stamping == {}
    assert DEST in Transport._path_request_times


def test_request_path_stamping_disabled_sends_48_byte_form_without_searching():
    ifs = _setup()
    Transport.stamp_path_requests = False
    real = stamps.find_pr_stamp
    stamps.find_pr_stamp = lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched"))
    try:
        Transport.request_path(DEST)
    finally:
        stamps.find_pr_stamp = real
    r = _sent_requests(ifs[0])[0]
    assert len(r) == 48
    assert Transport._parse_path_request(r) == (DEST, Transport.identity.hash, r[32:48], None)


def test_request_path_falls_back_to_unstamped_after_timeout():
    ifs = _setup()
    Transport.pr_stamp_timeout = 0.15
    real = stamps.find_pr_stamp
    n = [0]

    def never(*a, **k):
        n[0] += 1
        time.sleep(0.02)
        return None, 100
    stamps.find_pr_stamp = never
    try:
        t0 = time.time()
        Transport.request_path(DEST)
        dt = time.time() - t0
    finally:
        stamps.find_pr_stamp = real
    assert 0.14 <= dt < 1.0 and n[0] >= 2
    r = _sent_requests(ifs[0])
    assert len(r) == 1 and len(r[0]) == 48               # fell back to the unstamped form
    assert Transport._pr_stamping == {}


def test_request_path_import_failure_still_sends_unstamped():
    ifs = _setup()
    real = stamps.find_pr_stamp
    stamps.find_pr_stamp = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        Transport.request_path(DEST)
    finally:
        stamps.find_pr_stamp = real
    assert [len(x) for x in _sent_requests(ifs[0])] == [48]


def test_explicit_extension_is_sent_as_given_and_never_stamped():
    ifs = _setup()
    real = stamps.find_pr_stamp
    stamps.find_pr_stamp = lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched"))
    ext = bytes([0xC1, 0x00]) + b"\x99" * 14
    tag = b"\x77" * 16
    try:
        Transport.request_path(DEST, tag=tag, ext_payload=ext)
    finally:
        stamps.find_pr_stamp = real
    r = _sent_requests(ifs[0])[0]
    assert Transport._parse_path_request(r) == (DEST, Transport.identity.hash, tag, ext)


def test_stamp_search_does_not_block_event_loop_and_waiter_timeout_counts_from_send():
    """The search runs as a task in slices; the loop keeps ticking; an
    ensure_path waiter whose timeout (0.1 s) is shorter than the search (0.3 s)
    must NOT time out while the request is unsent, and its clock restarts when
    the request is actually sent. A re-request during the search is a no-op."""
    ifs = _setup()
    real = stamps.find_pr_stamp
    state = {"n": 0}

    def slow(dest, tag, *a, **k):
        state["n"] += 1
        time.sleep(0.03)                                   # one blocking slice
        if state["n"] >= 10:
            return b"\xaa" * 14, 5
        return None, 5
    stamps.find_pr_stamp = slow
    timeouts, found, gaps = [], [], []

    async def main():
        async def ticker():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.005)
                now = time.monotonic()
                gaps.append(now - last)
                last = now
        tk = asyncio.create_task(ticker())
        Transport.ensure_path(DEST, lambda: found.append(1), lambda: timeouts.append(time.time()),
                              timeout=0.1)
        assert DEST in Transport._pr_stamping
        t_start = time.time()
        Transport.request_path(DEST)                       # re-request while searching: no-op
        for _ in range(100):
            Transport._process_path_waiters()
            await asyncio.sleep(0.01)
            if _sent_requests(ifs[0]):
                break
        t_sent = time.time()
        assert t_sent - t_start >= 0.25                    # the search took ~0.3 s
        assert timeouts == [], "waiter timed out while the request was still being stamped"
        w = Transport._path_waiters[DEST][0]
        assert w["deadline"] >= t_sent + 0.09              # clock restarted at send time
        await asyncio.sleep(0.25)
        Transport._process_path_waiters()
        assert len(timeouts) == 1                          # ...and it still times out afterwards
        tk.cancel()

    stamps.reset_native_probe()
    try:
        asyncio.run(main())
    finally:
        stamps.find_pr_stamp = real
    assert state["n"] == 10
    assert len(_sent_requests(ifs[0])) >= 1
    assert max(gaps) < 0.1                                 # one slice (30 ms) + scheduling, never the whole search
    assert Transport._pr_stamping == {}


def test_concurrent_search_cap_sends_unstamped():
    ifs = _setup()
    Transport._pr_stamping = {b"\x01" * 16: True, b"\x02" * 16: True}
    Transport.request_path(DEST)
    assert [len(x) for x in _sent_requests(ifs[0])] == [48]
    Transport._pr_stamping = {}


# --------------------------------------------------------------------------
def _relay_forward(data, stamp_pr=True):
    ifs = _setup(2, transport=True)
    Transport.stamp_path_requests = stamp_pr

    class P:
        receiving_interface = ifs[0]
    Transport.path_request_handler(data, P())
    return ifs


def test_relay_forwards_stamp_extension_unchanged_with_original_tag():
    d, t, g, st, val, data = VECTORS[0]
    ifs = _relay_forward(bytes.fromhex(data))
    assert _sent_requests(ifs[0]) == []                   # not echoed to the requester's interface
    fw = _sent_requests(ifs[1])
    assert len(fw) == 1 and len(fw[0]) == 64
    dest, tid, tag, ext = Transport._parse_path_request(fw[0])
    assert dest == bytes.fromhex(d) and tag == bytes.fromhex(g)          # same tag (loop avoidance)
    assert tid == Transport.identity.hash                                # forwarded as OUR request
    assert ext == bytes([0xC1, 0x00]) + bytes.fromhex(st)                # extension untouched
    assert stamps.validate_pr_stamp(ext[2:], dest, tag)                  # still valid at the next hop


def test_relay_marks_unstamped_request_as_relayed_like_the_reference():
    dest, tid, tag = b"\x11" * 16, b"\x22" * 16, b"\x33" * 16
    ifs = _relay_forward(dest + tid + tag)
    fw = _sent_requests(ifs[1])
    assert len(fw) == 1 and len(fw[0]) == 64
    d, t, g, ext = Transport._parse_path_request(fw[0])
    assert (d, g) == (dest, tag)
    assert ext == bytes([0xC1, 0xFF]) + bytes(14)


def test_relay_without_stamping_forwards_unstamped_and_legacy_requests_plain():
    dest, tid, tag = b"\x11" * 16, b"\x22" * 16, b"\x33" * 16
    ifs = _relay_forward(dest + tid + tag, stamp_pr=False)
    fw = _sent_requests(ifs[1])
    assert len(fw) == 1 and len(fw[0]) == 48
    ifs = _relay_forward(dest + tag, stamp_pr=False)                      # legacy 32 B in
    fw = _sent_requests(ifs[1])
    assert len(fw) == 1 and len(fw[0]) == 48 and fw[0][32:] == tag


def test_leaf_does_not_forward_requests():
    d, t, g, st, val, data = VECTORS[1]
    ifs = _setup(2, transport=False)

    class P:
        receiving_interface = ifs[0]
    Transport.path_request_handler(bytes.fromhex(data), P())
    assert _sent_requests(ifs[0]) == [] and _sent_requests(ifs[1]) == []


class _HwFake:
    """Native-module stand-in that passes the start-up check."""
    def __init__(self, seen=None):
        self.seen = seen if seen is not None else []

    def hwselftest(self, material, n):
        return (0, 0, n)

    def hwprobe(self, op):
        return 1 if op == 0 else 0           # claim ok; sentinel gone after the firmware reset

    def hwstats(self, reset):
        return (0, 0, 0, 0)

    def hashcash(self, *a):
        self.seen.append(("hc", len(a)))
        return None, a[3]

    def x25519_stamp2_init(self, buf):
        return 1

    def x25519_stamp2(self, *a):
        self.seen.append(("ks", len(a)))
        return None, None, a[3]


# --------------------------------------------------------------------------
def test_hw_sha_switch_default_off_and_s3_only():
    import sys
    ed = importlib.import_module("urns.crypto.ed25519")
    old_nat, old = getattr(ed, "_native", None), stamps.HW_SHA
    ed._native = _HwFake()
    stamps.reset_native_probe()
    stamp_hw.hw_reset()
    assert stamps.HW_SHA is False and stamps._hw() == 0
    stamps.HW_SHA = True
    try:
        orig = getattr(sys.implementation, "_machine", None)
        try:
            sys.implementation._machine = "Generic ESP32 module with ESP32"
            assert stamps._hw() == 0                      # original ESP32: register map differs
            sys.implementation._machine = "Generic ESP32S3 module with ESP32S3"
            assert stamps._hw() == 1
            sys.implementation._machine = "Raspberry Pi Pico W with RP2040"
            assert stamps._hw() == 0
        finally:
            if orig is None:
                del sys.implementation._machine
            else:
                sys.implementation._machine = orig
    finally:
        stamps.HW_SHA = old
        ed._native = old_nat
        stamps.reset_native_probe()
        stamp_hw.hw_reset()


def test_hw_flag_reaches_native_searches_only_when_on():
    seen = []
    ed = importlib.import_module("urns.crypto.ed25519")
    old_nat, old_hw = getattr(ed, "_native", None), stamps.HW_SHA
    ed._native = _HwFake(seen)
    stamps.reset_native_probe()
    stamp_hw.hw_reset()
    stamps.set_table_size(4)
    try:
        stamps.HW_SHA = False
        stamps.find_hashcash(b"m", b"\xff" * 32, 14, 10)
        stamps.find_link_key(b"d" * 16, b"s" * 32, 3.0, 10)
        assert seen == [("hc", 5), ("ks", 5)]                 # no hw argument at all
        import sys
        orig = getattr(sys.implementation, "_machine", None)
        sys.implementation._machine = "ESP32S3"
        stamps.HW_SHA = True
        seen.clear()
        try:
            stamps.find_hashcash(b"m", b"\xff" * 32, 14, 10)
            stamps.find_link_key(b"d" * 16, b"s" * 32, 3.0, 10)
        finally:
            if orig is None:
                del sys.implementation._machine
            else:
                sys.implementation._machine = orig
        assert seen == [("hc", 6), ("ks", 6)]                 # hw=1 passed to both
    finally:
        ed._native = old_nat
        stamps.HW_SHA = old_hw
        stamps.reset_native_probe()
        stamp_hw.hw_reset()
        stamps.set_table_size(64)


def test_pace_sizes_slices_from_measured_duration():
    P = stamps.Pace(1000, 50, 20000)
    P.update(1000, 160000)                       # 1000 candidates took 160 ms (slow clock)
    assert P.n == 500                            # -> ~80 ms target
    P.update(500, 40000)                         # now 40 ms: faster, grow but at most 2x
    assert P.n == 1000
    P.update(1, 1000000)                         # a stalled slice floors the size
    assert P.n == 50
    P.update(10 ** 6, 1000)                      # absurd speed is capped
    assert P.n <= 20000
    n = P.n
    P.update(0, 1000)
    P.update(10, 0)
    assert P.n == n                              # no information: unchanged


def test_find_link_key_updates_slice_size_from_native_timing():
    class Fake:
        def x25519_stamp2_init(self, buf):
            return 1

        def x25519_stamp2(self, *a):
            return None, None, a[3]

    ed = importlib.import_module("urns.crypto.ed25519")
    old_nat, old_us = getattr(ed, "_native", None), stamps._ticks_us
    ed._native = Fake()
    stamps.reset_native_probe()
    stamps.set_table_size(4)
    try:
        first = stamps.slice_size()
        assert first == stamps.SLICE_NATIVE
        # make each call see exactly 160 ms between its two clock reads
        seq = iter([0, 160000, 160000, 320000, 320000, 480000])
        stamps._ticks_us = lambda: next(seq)
        stamps.find_link_key(b"d" * 16, b"s" * 32, 3.0, stamps.slice_size())
        n1 = stamps.slice_size()
        assert n1 == int(first * stamps.SLICE_TARGET_MS * 1000 // 160000)
        stamps.find_link_key(b"d" * 16, b"s" * 32, 3.0, n1)
        assert stamps.slice_size() == int(n1 * stamps.SLICE_TARGET_MS * 1000 // 160000)
    finally:
        ed._native = old_nat
        stamps._ticks_us = old_us
        stamps.reset_native_probe()
        stamps.set_table_size(64)


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
    import sys
    sys.exit(1 if _run() else 0)
