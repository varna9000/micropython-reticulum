# Announce ingress queue + clock re-base tests (host-side, crypto-free). Run:
#   python3 firmware/tests/test_announce_ingress.py
#
# The harness sets Transport.announce_inline = True for the legacy synchronous
# semantics; these tests flip it off to exercise the deferred path that
# production runs (inbound() queues, job_loop validates under a time budget).

import harness
from harness import (const, transport, packet, Transport, MockInterface,
                     Identity, reset_transport, set_identity,
                     build_announce_hdr1, build_announce_data)

DEST = b"\xD0" * 16


def _dest(i):
    return bytes([0xD0, i & 0xFF]) + b"\x00" * 14


def build_pkt(dest, emitted=1000):
    """An unpacked announce Packet (as inbound() would build it)."""
    p = packet.Packet(destination=None,
                      data=build_announce_hdr1(dest, data=build_announce_data(emitted=emitted)))
    assert p.unpack()
    p.hops += 1
    p.receiving_interface = None
    return p


def _deferred(fn):
    """Run fn with deferred announce processing, restore inline for others."""
    Transport.announce_inline = False
    try:
        fn()
    finally:
        Transport.announce_inline = True


def test_requested_announce_jumps_full_ingress_queue():
    """A busy hub keeps the ingress queue full; the answer to OUR path request
    must not queue behind it (seen live: replies waited 15-30 s or were
    tail-dropped, so request_path never resolved within the caller's timeout)."""
    def body():
        reset_transport()
        iface = MockInterface("tcp")
        Transport.interfaces = [iface]
        for i in range(const.MAX_ANNOUNCE_INGRESS + 8):
            Transport.inbound(build_announce_hdr1(
                _dest(i), data=build_announce_data(emitted=1000 + i)), iface)
        assert len(Transport.announce_ingress) == const.MAX_ANNOUNCE_INGRESS
        # An unrequested announce into a full queue evicts the OLDEST entry
        # and stays within the cap (it gets no priority).
        Transport.inbound(build_announce_hdr1(
            DEST, data=build_announce_data(emitted=2000)), iface)
        assert Transport.announce_ingress[-1].destination_hash == DEST
        assert len(Transport.announce_ingress) == const.MAX_ANNOUNCE_INGRESS
        Transport.announce_ingress.pop()      # undo, keep the original scenario
        Transport.announce_ingress.insert(0, build_pkt(_dest(0)))
        # Once we have asked for DEST, its announce takes the queue head.
        import time
        Transport._path_request_times[DEST] = time.time()   # what request_path records
        Transport.inbound(build_announce_hdr1(
            DEST, data=build_announce_data(emitted=2001)), iface)
        assert Transport.announce_ingress[0].destination_hash == DEST
        # Service only the head: a draining loop on a fast host would LRU-evict
        # the first-handled path once the other 32 announces land after it.
        Transport._handle_announce(Transport.announce_ingress.pop(0))
        assert Transport.has_path(DEST)
    _deferred(body)


def test_unrequested_announce_gets_no_priority_after_window():
    def body():
        reset_transport()
        iface = MockInterface("tcp")
        Transport.interfaces = [iface]
        Transport._path_request_times[DEST] = 1.0     # requested long ago
        Transport.inbound(build_announce_hdr1(
            _dest(1), data=build_announce_data(emitted=1000)), iface)
        Transport.inbound(build_announce_hdr1(
            DEST, data=build_announce_data(emitted=1001)), iface)
        assert Transport.announce_ingress[0].destination_hash == _dest(1)
    _deferred(body)


def test_announce_deferred_to_ingress_queue():
    def body():
        reset_transport()
        iface = MockInterface("tcp")
        Transport.interfaces = [iface]
        raw = build_announce_hdr1(DEST, data=build_announce_data(emitted=1000))
        Transport.inbound(raw, iface)
        # Not processed yet: queued, no path installed.
        assert DEST not in Transport.path_table
        assert len(Transport.announce_ingress) == 1
        # job_loop's servicing installs it.
        Transport._service_announce_ingress()
        assert len(Transport.announce_ingress) == 0
        assert DEST in Transport.path_table
        assert Transport.path_table[DEST][const.IDX_PT_HOPS] == 1
    _deferred(body)


def test_ingress_queue_bounded_drops_overflow():
    def body():
        reset_transport()
        iface = MockInterface("tcp")
        Transport.interfaces = [iface]
        n = const.MAX_ANNOUNCE_INGRESS + 8
        for i in range(n):
            raw = build_announce_hdr1(_dest(i),
                                      data=build_announce_data(emitted=1000 + i))
            Transport.inbound(raw, iface)
        assert len(Transport.announce_ingress) == const.MAX_ANNOUNCE_INGRESS
        # Overflow evicted the OLDEST 8 (freshest data wins); the rest process.
        queued = [p.destination_hash for p in Transport.announce_ingress]
        assert queued == [_dest(i) for i in range(8, n)]
        assert Transport.ann_stats[5] == 8
        while Transport.announce_ingress:
            Transport._service_announce_ingress()
        assert len(Transport.path_table) == const.MAX_ANNOUNCE_INGRESS
    _deferred(body)


def test_ingress_error_does_not_wedge_queue():
    def body():
        reset_transport()
        iface = MockInterface("tcp")
        Transport.interfaces = [iface]
        good = build_announce_hdr1(DEST, data=build_announce_data(emitted=1000))
        Transport.inbound(good, iface)
        # A queue entry whose processing raises must be discarded, not retried.
        class Boom:
            packet_type = const.PKT_ANNOUNCE
            destination_hash = b"\xEE" * 16
            def __getattr__(self, name):
                raise RuntimeError("corrupt packet")
        Transport.announce_ingress.insert(0, Boom())
        Transport._service_announce_ingress()
        assert len(Transport.announce_ingress) == 0
        assert DEST in Transport.path_table
    _deferred(body)


def test_inline_mode_processes_synchronously():
    reset_transport()
    iface = MockInterface("tcp")
    Transport.interfaces = [iface]
    raw = build_announce_hdr1(DEST, data=build_announce_data(emitted=1000))
    Transport.inbound(raw, iface)          # harness default: inline
    assert DEST in Transport.path_table
    assert len(Transport.announce_ingress) == 0


def test_shift_clocks_rebases_pre_sync_stamps():
    import time
    reset_transport()
    iface = MockInterface("tcp")
    Transport.interfaces = [iface]
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)),
                      iface)
    e = Transport.path_table[DEST]
    ts0 = e[const.IDX_PT_TIMESTAMP]
    exp0 = e[const.IDX_PT_EXPIRES]
    now = time.time()
    delta = 800000000                       # ~25 years: the boot RTC jump
    Transport._shift_clocks(delta, now + const.PATH_EXPIRY + 60)
    assert e[const.IDX_PT_TIMESTAMP] == ts0 + delta
    assert e[const.IDX_PT_EXPIRES] == exp0 + delta
    assert Transport.reachable_destinations[DEST] > delta
    # Post-sync stamps (>= cut) must NOT be shifted again.
    e[const.IDX_PT_EXPIRES] = now + delta + 100
    Transport._shift_clocks(delta, now + const.PATH_EXPIRY + 60)
    assert e[const.IDX_PT_EXPIRES] == now + delta + 100


def test_shift_clocks_survives_expiry_cull():
    """The actual failure: pre-sync paths must survive the first cull after
    the clock jump."""
    import time
    reset_transport()
    iface = MockInterface("tcp")
    Transport.interfaces = [iface]
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)),
                      iface)
    delta = 800000000
    # Simulate the jump: entry stamps stay old, "now" moves forward. Without
    # re-basing, expiry (old_now + PATH_EXPIRY) << new now -> culled.
    e = Transport.path_table[DEST]
    assert e[const.IDX_PT_EXPIRES] < time.time() + delta
    Transport._shift_clocks(delta, time.time() + const.PATH_EXPIRY + 60)
    assert e[const.IDX_PT_EXPIRES] > time.time() + delta - 120


def test_shift_clocks_rebases_link_activity():
    """A link's stale check reads last_activity; the boot clock jump must not
    make a link that was live a second ago look ~25 years idle (seen live:
    the peer's link was torn down as stale the second the clock synced)."""
    import time
    reset_transport()

    class L:
        pass
    l = L()
    now = time.time()
    for a in ("last_activity", "last_outbound", "_last_keepalive", "_lrrtt_last"):
        setattr(l, a, now - 5)
    Transport.active_links = [l]
    delta = 800000000
    Transport._shift_clocks(delta, now + const.PATH_EXPIRY + 60)
    for a in ("last_activity", "last_outbound", "_last_keepalive", "_lrrtt_last"):
        assert getattr(l, a) == now - 5 + delta, a
    Transport.active_links = []


PUB = b"\x11" * 64        # build_announce_data's default public key


def _install_known(dest, hops_raw=0, emitted=1000, iface=None):
    """Install a path for `dest` (processed inline) and the matching known key."""
    import time
    Transport.announce_inline = True
    Transport.inbound(build_announce_hdr1(
        dest, data=build_announce_data(emitted=emitted), hops=hops_raw), iface)
    Transport.announce_inline = False
    Transport.announce_table.clear()      # relay rebroadcast entry (exempt) not under test
    assert dest in Transport.path_table
    Identity.known_destinations[dest] = [time.time() - 100, b"", PUB, None, None]
    return Transport.path_table[dest]


def _ctx_resp(raw):
    return raw[:18] + bytes([const.CTX_PATH_RESPONSE]) + raw[19:]


def test_noop_announce_not_queued_but_refreshes_route():
    def body():
        reset_transport()
        iface = MockInterface("tcp")
        Transport.interfaces = [iface]
        entry = _install_known(DEST, hops_raw=0, emitted=1000, iface=iface)
        entry[const.IDX_PT_EXPIRES] = 5.0          # about to lapse
        entry[const.IDX_PT_TIMESTAMP] = 5.0
        Transport.reachable_destinations[DEST] = 1.0
        kd_ts = Identity.known_destinations[DEST][0]
        # Same emission, same hops (a hub re-sending the very announce it
        # already told us) changes nothing: dropped before validation, twice.
        for em in (1000, 1000):
            Transport.inbound(build_announce_hdr1(
                DEST, data=build_announce_data(emitted=em)), iface)
        assert Transport.announce_ingress == []
        assert Transport.ann_stats[0] == 2 and Transport.ann_stats[3] == 2
        # ...but the route was re-confirmed in place (keep-alive), as the
        # queued path would have done.
        assert entry[const.IDX_PT_EXPIRES] > 1e9
        assert Transport.reachable_destinations[DEST] > 1e9
        assert Identity.known_destinations[DEST][0] > kd_ts
        assert entry[const.IDX_PT_EMITTED] == 1000
    _deferred(body)


def test_announce_that_can_change_state_is_queued():
    def body():
        reset_transport()
        iface = MockInterface("tcp")
        Transport.interfaces = [iface]
        _install_known(DEST, hops_raw=2, emitted=1000, iface=iface)      # 3 hops
        # newer emission, equal hops
        Transport.inbound(build_announce_hdr1(
            DEST, data=build_announce_data(emitted=1001), hops=2), iface)
        assert len(Transport.announce_ingress) == 1
        Transport.announce_ingress.clear()
        # fewer hops, OLDER emission (shortest path wins in should_add)
        Transport.inbound(build_announce_hdr1(
            DEST, data=build_announce_data(emitted=900), hops=0), iface)
        assert len(Transport.announce_ingress) == 1
        Transport.announce_ingress.clear()
        # more hops, emission not newer: no-op while the shorter path is alive
        Transport.inbound(build_announce_hdr1(
            DEST, data=build_announce_data(emitted=1000), hops=5), iface)
        assert Transport.announce_ingress == []
        # ...but adopted once the current path has expired
        Transport.path_table[DEST][const.IDX_PT_EXPIRES] = 1.0
        Transport.inbound(build_announce_hdr1(
            DEST, data=build_announce_data(emitted=2001), hops=5), iface)
        assert len(Transport.announce_ingress) == 1
    _deferred(body)


def test_prefilter_exemptions():
    """Never filtered: path responses, destinations we asked for / are
    discovering / are rebroadcasting, a key mismatch, an unknown key."""
    def body():
        import time
        reset_transport()
        iface = MockInterface("tcp")
        Transport.interfaces = [iface]
        _install_known(DEST, emitted=1000, iface=iface)
        same = build_announce_hdr1(DEST, data=build_announce_data(emitted=1000))
        # path response (context 0x0B), same emission
        Transport.inbound(_ctx_resp(same), iface)
        assert len(Transport.announce_ingress) == 1
        Transport.announce_ingress.clear()
        # destination in announce_table / discovery_path_requests
        Transport.announce_table[DEST] = [0] * 9
        Transport.inbound(same, iface)
        assert len(Transport.announce_ingress) == 1
        Transport.announce_ingress.clear()
        del Transport.announce_table[DEST]
        Transport.discovery_path_requests[DEST] = {"requesting_interface": None, "timeout": 1e12}
        Transport.inbound(same, iface)
        assert len(Transport.announce_ingress) == 1
        Transport.announce_ingress.clear()
        del Transport.discovery_path_requests[DEST]
        # awaited (we sent a path request)
        Transport._path_request_times[DEST] = time.time()
        Transport.inbound(same, iface)
        assert len(Transport.announce_ingress) == 1
        Transport.announce_ingress.clear()
        del Transport._path_request_times[DEST]
        # key differs from the one we hold -> full validation decides
        Identity.known_destinations[DEST][2] = b"\x77" * 64
        Transport.inbound(same, iface)
        assert len(Transport.announce_ingress) == 1
        Transport.announce_ingress.clear()
        # path but no stored key (e.g. persisted path table) -> relearn it
        del Identity.known_destinations[DEST]
        Transport.inbound(same, iface)
        assert len(Transport.announce_ingress) == 1
    _deferred(body)


def test_same_announce_is_deduplicated_but_different_ones_queue_beside_each_other():
    def body():
        reset_transport()
        iface = MockInterface("tcp")
        Transport.interfaces = [iface]
        a, b = _dest(1), _dest(2)
        Transport.inbound(build_announce_hdr1(a, data=build_announce_data(emitted=1000), hops=2), iface)
        Transport.inbound(build_announce_hdr1(b, data=build_announce_data(emitted=1000)), iface)
        # a DIFFERENT announce (newer emission) is queued BESIDE the first: nothing is
        # authenticated yet, so it may not displace the queued one
        Transport.inbound(build_announce_hdr1(a, data=build_announce_data(emitted=1001), hops=2), iface)
        assert [p.destination_hash for p in Transport.announce_ingress] == [a, b, a]
        assert [Transport._announce_emitted(p) for p in Transport.announce_ingress if p.destination_hash == a] == [1000, 1001]
        # the SAME announce over a shorter path: one copy, fewer hops, same slot
        Transport.inbound(build_announce_hdr1(a, data=build_announce_data(emitted=1000), hops=0), iface)
        assert [p.hops for p in Transport.announce_ingress if p.destination_hash == a] == [1, 3]
        # same announce over a longer path: ignored
        Transport.inbound(build_announce_hdr1(a, data=build_announce_data(emitted=1000), hops=5), iface)
        assert [p.hops for p in Transport.announce_ingress if p.destination_hash == a] == [1, 3]
        # per-destination bound: a third different one fits, a fourth is dropped
        Transport.inbound(build_announce_hdr1(a, data=build_announce_data(emitted=500), hops=0), iface)
        Transport.inbound(build_announce_hdr1(a, data=build_announce_data(emitted=2000), hops=0), iface)
        assert len([p for p in Transport.announce_ingress if p.destination_hash == a]) == const.MAX_ANNOUNCE_PER_DEST
        assert len(Transport.announce_ingress) == const.MAX_ANNOUNCE_PER_DEST + 1
    _deferred(body)


def test_awaited_duplicate_moves_to_head():
    def body():
        import time
        reset_transport()
        iface = MockInterface("tcp")
        Transport.interfaces = [iface]
        Transport.inbound(build_announce_hdr1(_dest(1), data=build_announce_data(emitted=1000)), iface)
        Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)), iface)
        assert Transport.announce_ingress[-1].destination_hash == DEST
        Transport._path_request_times[DEST] = time.time()
        Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)), iface)
        assert [p.destination_hash for p in Transport.announce_ingress] == [DEST, _dest(1)]
    _deferred(body)


def test_drain_is_budgeted_but_always_progresses():
    def body():
        import time
        reset_transport()
        iface = MockInterface("tcp")
        Transport.interfaces = [iface]
        for i in range(10):
            Transport.inbound(build_announce_hdr1(
                _dest(i), data=build_announce_data(emitted=1000 + i)), iface)
        real = Transport._handle_announce
        Transport._handle_announce = staticmethod(lambda p: time.sleep(0.03))
        try:
            Transport._service_announce_ingress()        # 20 ms budget < 30 ms item
            assert len(Transport.announce_ingress) == 9   # exactly one of progress
            assert Transport.ann_stats[6] == 1
        finally:
            Transport._handle_announce = real
        saved = const.ANNOUNCE_INGRESS_BUDGET_MS
        const.ANNOUNCE_INGRESS_BUDGET_MS = 10000
        try:
            Transport._service_announce_ingress()
            assert Transport.announce_ingress == []
        finally:
            const.ANNOUNCE_INGRESS_BUDGET_MS = saved
    _deferred(body)


def test_job_loop_drains_faster_than_one_slice_per_tick():
    """With a 30 ms/announce validator the old one-slice-per-250 ms-tick policy
    cleared ~2 per tick (~4 in 0.5 s). Draining in short yields must do far
    better while still yielding to other tasks."""
    import asyncio, time
    reset_transport()
    iface = MockInterface("tcp")
    Transport.interfaces = [iface]
    Transport.announce_inline = False
    for i in range(32):
        Transport.inbound(build_announce_hdr1(
            _dest(i), data=build_announce_data(emitted=1000 + i)), iface)
    real = Transport._handle_announce
    Transport._handle_announce = staticmethod(lambda p: time.sleep(0.01))
    gaps = []

    async def main():
        async def ticker():
            last = time.monotonic()
            while Transport._jobs_running:
                await asyncio.sleep(0.005)
                now = time.monotonic()
                gaps.append(now - last)
                last = now
        Transport._jobs_running = True
        t1 = asyncio.create_task(Transport.job_loop())
        t2 = asyncio.create_task(ticker())
        await asyncio.sleep(0.5)
        Transport._jobs_running = False
        await asyncio.sleep(0.3)
        t1.cancel(); t2.cancel()

    try:
        asyncio.run(main())
    finally:
        Transport._handle_announce = real
        Transport.announce_inline = True
    processed = Transport.ann_stats[6]
    assert processed >= 12, processed
    # one slice = 20 ms budget + at most one 10 ms item; allow scheduler noise
    assert max(gaps) < 0.08, max(gaps)



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
