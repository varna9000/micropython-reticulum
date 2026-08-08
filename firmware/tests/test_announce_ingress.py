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


def _deferred(fn):
    """Run fn with deferred announce processing, restore inline for others."""
    Transport.announce_inline = False
    try:
        fn()
    finally:
        Transport.announce_inline = True


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
        # Overflow dropped the newest; the queued ones all process.
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
