# reachable_destinations lifetime tests (host-side, crypto-free). Run:
#   python3 firmware/tests/test_reachability.py
#
# has_path() reads reachable_destinations, a store separate from path_table.
# On a public-hub feed the two desynced: membership was only (re)written when
# a route was ADOPTED, so once the cap evicted a destination (by arbitrary
# dict order on MicroPython), a replayed path response — same emission, hence
# the should_add=False keep-alive branch — could never restore it, and
# has_path() stayed false-negative for a destination with a live, refreshed
# route ("no path to node" while the network answered every request). These
# tests pin the repaired lifetime: refresh on every validated announce,
# LRU-by-last-heard eviction, and removal together with the route.

import harness  # noqa: F401
from harness import (const, Transport, MockInterface, reset_transport,
                     build_announce_hdr1, build_announce_data)

A = b"\xA0" * 16
B = b"\xB0" * 16
C = b"\xC0" * 16
D = b"\xD0" * 16


def _announce(dest, iface, emitted=1000):
    Transport.inbound(
        build_announce_hdr1(dest, data=build_announce_data(emitted=emitted)),
        iface)


def _setup():
    reset_transport()
    iface = MockInterface("tcp")
    Transport.interfaces = [iface]
    return iface


def test_dup_announce_restores_membership():
    """THE field failure: membership evicted while the route lives; the
    replayed path response (same emission -> should_add False) must still
    restore has_path()."""
    iface = _setup()
    _announce(A, iface)
    assert Transport.has_path(A)
    entry = Transport.path_table[A]

    # Simulate the cap eviction of the old code: membership gone, route alive.
    Transport.reachable_destinations.pop(A)
    assert not Transport.has_path(A)

    # The path response replays the SAME announce (packet_filter exempts
    # duplicate SINGLE announces, so it reaches _handle_announce).
    _announce(A, iface)
    assert Transport.has_path(A), "dup announce must restore reachability"
    assert Transport.path_table[A] is entry, "route must be kept, not rebuilt"


def test_reachable_eviction_is_lru_by_last_heard():
    """Eviction must drop the least-recently-HEARD destination — and a dup
    announce counts as heard, protecting refreshed entries."""
    iface = _setup()
    saved = const.MAX_DESTINATIONS
    const.MAX_DESTINATIONS = 3
    try:
        _announce(A, iface, emitted=1000)
        _announce(B, iface, emitted=1001)
        _announce(C, iface, emitted=1002)
        rd = Transport.reachable_destinations
        rd[A] -= 100                    # A is stalest...
        _announce(A, iface, emitted=1000)   # ...but a dup refreshes it
        rd[B] -= 50                     # now B is stalest
        _announce(D, iface, emitted=1003)   # full: must evict B, not A
        assert set(rd) == {A, C, D}
    finally:
        const.MAX_DESTINATIONS = saved


def test_path_cap_eviction_drops_reachability():
    """Evicting a route must evict membership too: has_path() True with no
    route makes callers skip the path request and emit an un-forwardable
    HDR_1 link request. A later announce fully re-learns the destination."""
    iface = _setup()
    saved = const.MAX_PATH_TABLE
    const.MAX_PATH_TABLE = 2
    try:
        _announce(A, iface, emitted=1000)
        _announce(B, iface, emitted=1001)
        Transport.path_table[A][const.IDX_PT_TIMESTAMP] -= 10   # A is oldest
        _announce(C, iface, emitted=1002)
        assert A not in Transport.path_table
        assert not Transport.has_path(A)
        # Recovery: the replayed announce re-installs route + membership.
        _announce(A, iface, emitted=1000)
        assert A in Transport.path_table
        assert Transport.has_path(A)
    finally:
        const.MAX_PATH_TABLE = saved


def test_expire_path_drops_reachability():
    iface = _setup()
    _announce(A, iface)
    Transport.expire_path(A)
    assert A not in Transport.path_table
    assert not Transport.has_path(A)
    _announce(A, iface)
    assert Transport.has_path(A)


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
