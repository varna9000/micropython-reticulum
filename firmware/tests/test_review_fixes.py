# Code-review fixes for the RNS 1.6 stamp work (host-side). Run:
#   python3 firmware/tests/test_review_fixes.py
# One section per finding (F1..F9; F10 is in test_tcp_backoff.py).

import asyncio
import hashlib
import importlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
from harness import (const, packet, Transport, MockInterface, Identity, reset_transport,
                     build_announce_hdr1, build_announce_data)
import test_path_request_stamps as TP
import test_announce_ingress as TI
import test_lxmf_tickets as TL
import test_announce_stamps as TA
import test_stamps as TS
import announce_stamp_support as S

stamps = importlib.import_module("urns.stamps")
transport_mod = importlib.import_module("urns.transport")
lxmf = importlib.import_module("urns.lxmf")
link = harness.link
umsgpack = importlib.import_module("urns.umsgpack")
A = S.announce_stamps


class FakeClock:
    """Replaces transport's `time` module: a controllable wall clock."""
    t = 5000.0

    @staticmethod
    def time():
        return FakeClock.t

    def __getattr__(self, n):
        return getattr(time, n)


def _fake_transport_time():
    real = transport_mod.time

    class T:
        time = staticmethod(lambda: FakeClock.t)
        def __getattr__(self, n):
            return getattr(real, n)
    transport_mod.time = T()
    return real


# ---------------------------------------------------------------- F1 ----
def test_f1_ensure_path_waiters_time_out_and_requests_stop_with_stamped_requests():
    ifs = TP._setup()
    Transport.pr_stamp_timeout = 0          # search ends at once -> after_search send each time
    real_time = _fake_transport_time()
    FakeClock.t = 5000.0
    fired = []
    try:
        Transport.ensure_path(TP.DEST, lambda: fired.append("found"),
                              lambda: fired.append("timeout"), timeout=15)
        w = Transport._path_waiters[TP.DEST][0]
        assert w["hard"] == 5000.0 + 15 + 0
        n_sent = []
        for step in range(40):                       # 40 x 5 s of job_loop
            FakeClock.t += 5
            Transport._process_path_waiters()
            n_sent.append(len(TP._sent_requests(ifs[0])))
            if fired:
                break
        assert fired == ["timeout"], fired
        assert FakeClock.t - 5000.0 <= 15 + 5 + 1, FakeClock.t - 5000.0
        assert TP.DEST not in Transport._path_waiters
        before = len(TP._sent_requests(ifs[0]))
        for _ in range(10):                          # no waiters left: no more requests
            FakeClock.t += 5
            Transport._process_path_waiters()
        assert len(TP._sent_requests(ifs[0])) == before
    finally:
        transport_mod.time = real_time


def test_f1_deadline_extended_for_stamp_search_only_once():
    ifs = TP._setup()
    Transport.pr_stamp_timeout = 30
    real_time = _fake_transport_time()
    FakeClock.t = 7000.0
    try:
        w = {"on_found": None, "on_timeout": None, "deadline": 7015.0, "timeout": 15,
             "hard": 7000.0 + 15 + 30}
        Transport._path_waiters[TP.DEST] = [w]
        FakeClock.t = 7020.0                               # first send, after a 20 s search
        Transport._send_path_request(TP.DEST, b"\x01" * 16, None, None, True)
        assert w["deadline"] == 7035.0 and w["stretched"]
        FakeClock.t = 7030.0                               # a later re-request must not extend again
        Transport._send_path_request(TP.DEST, b"\x02" * 16, None, None, True)
        assert w["deadline"] == 7035.0
        # and never beyond the hard bound
        w2 = {"deadline": 0, "timeout": 100, "hard": 7040.0}
        Transport._path_waiters[TP.DEST] = [w2]
        Transport._send_path_request(TP.DEST, b"\x03" * 16, None, None, True)
        assert w2["deadline"] == 7040.0
    finally:
        transport_mod.time = real_time
        Transport._path_waiters = {}


def test_f1_hard_bound_applies_even_while_a_stamp_search_is_still_running():
    TP._setup()
    Transport.pr_stamp_timeout = 30
    real_time = _fake_transport_time()
    FakeClock.t = 9000.0
    fired = []
    try:
        Transport._pr_stamping[TP.DEST] = True               # a search that never ends
        Transport._path_waiters[TP.DEST] = [{
            "on_found": None, "on_timeout": lambda: fired.append(1), "deadline": 9015.0,
            "timeout": 15, "hard": 9045.0}]
        FakeClock.t = 9030.0
        Transport._process_path_waiters()
        assert not fired                                      # still within the search allowance
        FakeClock.t = 9046.0
        Transport._process_path_waiters()
        assert fired == [1] and TP.DEST not in Transport._path_waiters
    finally:
        transport_mod.time = real_time
        Transport._pr_stamping = {}


# ---------------------------------------------------------------- F2 ----
def _deferred_setup():
    reset_transport()
    iface = MockInterface("tcp")
    Transport.interfaces = [iface]
    Transport.announce_inline = False
    return iface


def _flood(iface, n, base=100):
    for i in range(n):
        Transport.inbound(build_announce_hdr1(
            bytes([0xE0, (base + i) & 0xFF]) + b"\x00" * 14,
            data=build_announce_data(emitted=1000 + i)), iface)


def test_f2_awaited_entry_survives_full_queue_and_awaited_overflow_is_bounded():
    iface = _deferred_setup()
    try:
        _flood(iface, const.MAX_ANNOUNCE_INGRESS)
        assert len(Transport.announce_ingress) == const.MAX_ANNOUNCE_INGRESS
        awaited = b"\xAA" * 16
        Transport._path_request_times[awaited] = time.time()
        Transport.inbound(build_announce_hdr1(awaited, data=build_announce_data(emitted=5000)), iface)
        assert Transport.announce_ingress[0].destination_hash == awaited
        _flood(iface, 3 * const.MAX_ANNOUNCE_INGRESS, base=10)    # lots of unsolicited arrivals
        assert awaited in [p.destination_hash for p in Transport.announce_ingress], \
            "the awaited path answer was evicted"
        # many awaited answers: bounded (cap + small constant), never evicting each other
        for i in range(20):
            d = bytes([0xBB, i]) + b"\x00" * 14
            Transport._path_request_times[d] = time.time()
            Transport.inbound(build_announce_hdr1(d, data=build_announce_data(emitted=6000)), iface)
        assert len(Transport.announce_ingress) <= const.MAX_ANNOUNCE_INGRESS + transport_mod._ANNOUNCE_AWAITED_EXTRA
        assert awaited in [p.destination_hash for p in Transport.announce_ingress]
    finally:
        Transport.announce_inline = True


# ---------------------------------------------------------------- F8 ----
def test_f8_awaited_destination_keeps_better_copy():
    iface = _deferred_setup()
    try:
        d = b"\xAC" * 16
        Transport._path_request_times[d] = time.time()
        Transport.inbound(build_announce_hdr1(d, data=build_announce_data(emitted=1000), hops=0), iface)   # 1 hop
        Transport.inbound(build_announce_hdr1(d, data=build_announce_data(emitted=1000), hops=4), iface)   # 5 hops
        q = [p for p in Transport.announce_ingress if p.destination_hash == d]
        assert len(q) == 1 and q[0].hops == 1, [p.hops for p in q]
        # a DIFFERENT (newer) announce is queued beside it, at the head: nothing is validated yet
        Transport.inbound(build_announce_hdr1(d, data=build_announce_data(emitted=1001), hops=0), iface)
        q = [p for p in Transport.announce_ingress if p.destination_hash == d]
        assert sorted(Transport._announce_emitted(p) for p in q) == [1000, 1001]
        assert Transport.announce_ingress[0].destination_hash == d     # awaited -> head
    finally:
        Transport.announce_inline = True


# ---------------------------------------------------------------- F9 ----
def test_f9_newer_emission_over_longer_path_is_validated_but_route_kept():
    reset_transport()
    iface = MockInterface("tcp")
    Transport.interfaces = [iface]
    dest = b"\xD9" * 16
    entry = TI._install_known(dest, hops_raw=0, emitted=1000, iface=iface)   # 1 hop
    calls = []
    real = Identity.validate_announce

    def rec(packet_, only_validate_signature=False):
        calls.append(Transport._announce_emitted(packet_))
        return True
    Identity.validate_announce = staticmethod(rec)
    Transport.announce_inline = False
    try:
        Transport.inbound(build_announce_hdr1(dest, data=build_announce_data(emitted=2000), hops=4), iface)
        assert len(Transport.announce_ingress) == 1, "newer emission must not be dropped pre-validation"
        Transport._service_announce_ingress()
        assert calls == [2000]
        assert Transport.path_table[dest][const.IDX_PT_HOPS] == 1          # route unchanged
        # the same emission again (another longer copy): nothing new
        Transport.inbound(build_announce_hdr1(dest, data=build_announce_data(emitted=2000), hops=6), iface)
        assert Transport.announce_ingress == []
        # an older one is a DIFFERENT announce: validated (identity data), not skipped as "not newer"
        Transport.inbound(build_announce_hdr1(dest, data=build_announce_data(emitted=1500), hops=6), iface)
        assert len(Transport.announce_ingress) == 1
        Transport._service_announce_ingress()
        assert calls == [2000, 1500]
        Transport.inbound(build_announce_hdr1(dest, data=build_announce_data(emitted=1500), hops=6), iface)
        assert Transport.announce_ingress == []          # now a true duplicate
    finally:
        Identity.validate_announce = real
        Transport.announce_inline = True


# ---------------------------------------------------------------- F5 ----
def test_f5_stamp_task_setup_failure_sends_unstamped_and_clears_flag():
    ifs = TP._setup()
    real_duty = stamps.Duty

    def boom(*a, **k):
        raise MemoryError("no heap")
    stamps.Duty = boom

    async def main():
        Transport.request_path(TP.DEST)
        assert TP.DEST in Transport._pr_stamping
        for _ in range(100):
            await asyncio.sleep(0.01)
            if TP.DEST not in Transport._pr_stamping:
                break
    try:
        asyncio.run(main())
    finally:
        stamps.Duty = real_duty
    assert Transport._pr_stamping == {}, "flag stuck: later path requests would be no-ops"
    assert [len(x) for x in TP._sent_requests(ifs[0])] == [48]
    Transport.request_path(TP.DEST)                      # works again (inline: no loop)
    assert len(TP._sent_requests(ifs[0])) == 2


# ---------------------------------------------------------------- F6 ----
def test_f6_stamp_path_requests_default_needs_native_hashcash():
    saved = (stamps._hcmod, stamps._hc_state)
    try:
        stamps._hcmod, stamps._hc_state = None, -1
        assert stamps.have_hashcash() is False
        class Nat:
            def hashcash(self, *a):
                return None, 0
        stamps._hcmod, stamps._hc_state = Nat(), 1
        assert stamps.have_hashcash() is True
    finally:
        stamps._hcmod, stamps._hc_state = saved
    assert not hasattr(stamps, "path_requests_enabled"), "the default rule lives in reticulum.py only"
    src = open(os.path.join(harness.__file__.rsplit("/tests/", 1)[0], "urns", "reticulum.py")).read()
    # boot code probes crypto.ed25519, never importing stamps (see test_stamp_imports.py)
    assert "_ed.hashcash_module()" in src and "import stamps as _stp" not in src
    assert 'Transport.stamp_path_requests = _hc if _spr is None else bool(_spr)' in src
    assert Transport.stamp_path_requests in (None, True, False)


# ---------------------------------------------------------------- F7 ----
def _gen_setup(n):
    tmp = TA._tmp()
    A.configure(enabled=True, value=12, allow_low=True, duty=50)
    S.Transport.interfaces = []
    return [S.make_destination(app="gen%d" % i) for i in range(n)], tmp


def test_f7_one_announce_stamp_generation_at_a_time():
    dests, tmp = _gen_setup(3)
    seq = []
    real = stamps.find_hashcash
    counts = {}

    def spy(material, target, slen, budget=None, background=False):
        counts[material] = counts.get(material, 0) + 1
        seq.append(material)
        if counts[material] < 4:
            return None, 10                      # takes several slices
        return real(material, target, slen, 5000)

    stamps.find_hashcash = spy

    async def main():
        for d in dests:
            A.request(d)
        t0 = time.time()
        while A._tasks and time.time() - t0 < 30:
            await asyncio.sleep(0.01)
    try:
        asyncio.run(main())
    finally:
        stamps.find_hashcash = real
    assert not A._tasks and len(set(seq)) == 3
    runs = [seq[0]]
    for m in seq[1:]:
        if m != runs[-1]:
            runs.append(m)
    assert len(runs) == 3, "generations interleaved: %d runs" % len(runs)
    for d in dests:
        assert A.get_stamp(d) is not None
    A.reset()


def test_f7_background_generation_yields_to_interactive_searches():
    dests, tmp = _gen_setup(1)
    real = stamps.find_hashcash
    calls = []

    def spy(material, target, slen, budget=None, background=False):
        calls.append(time.time())
        return real(material, target, slen, 5000)
    stamps.find_hashcash = spy

    async def main():
        with stamps.interactive:
            A.request(dests[0])
            await asyncio.sleep(0.4)
            during = len(calls)
        t0 = time.time()
        while A._tasks and time.time() - t0 < 20:
            await asyncio.sleep(0.01)
        return during
    try:
        during = asyncio.run(main())
    finally:
        stamps.find_hashcash = real
    assert during == 0, "background search ran during an interactive search"
    assert calls and A.get_stamp(dests[0]) is not None
    assert not stamps.interactive_active()
    A.reset()


def test_f7_interactive_flag_released_on_every_exit_path():
    ifs = TP._setup()
    real = stamps.find_pr_stamp
    stamps.find_pr_stamp = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    try:
        Transport.request_path(TP.DEST)                 # inline path
    finally:
        stamps.find_pr_stamp = real
    assert not stamps.interactive_active()


# ---------------------------------------------------------------- F3 ----
def _lsetup(cost):
    TS._force_pure()
    iface = TS._link_setup()
    harness.Identity.link_costs[TS.DEST] = cost
    return iface


def test_f3_link_cost_above_limit_fails_fast_without_searching():
    iface = _lsetup(40.0)
    real = stamps.find_link_key
    stamps.find_link_key = lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched"))
    closed = []
    try:
        ol = link.OutgoingLink(TS._Dest(), closed_callback=lambda l: closed.append(l.close_reason))
    finally:
        stamps.find_link_key = real
    assert closed == ["stamp"] and ol.status == link.OutgoingLink.CLOSED
    assert iface.sent == [] and ol not in Transport.pending_links
    assert not stamps.interactive_active()


def test_f3_default_limits_follow_search_speed_and_config_overrides():
    TS._force_pure()
    assert stamps.default_max_link_cost() == stamps.MAX_LINK_COST_PY
    saved = stamps._native_state
    stamps._native_state, stamps._nmod = 1, object()
    try:
        assert stamps.default_max_link_cost() == stamps.MAX_LINK_COST_NATIVE
    finally:
        stamps._native_state, stamps._nmod = saved, None
    # cost 14 > pure default (12): refused; with max_stamp_cost=20 it is attempted
    iface = _lsetup(14.0)
    closed = []
    link.OutgoingLink(TS._Dest(), closed_callback=lambda l: closed.append(1))
    assert closed == [1]
    iface = _lsetup(14.0)
    closed = []
    real = stamps.find_link_key
    stamps.find_link_key = lambda *a, **k: (None, None, 1)
    link.OutgoingLink.max_stamp_cost = 20.0
    try:
        ol = link.OutgoingLink(TS._Dest(), closed_callback=lambda l: closed.append(l.close_reason),
                               stamp_timeout=0.2)          # no loop: inline search until the budget ends
    finally:
        link.OutgoingLink.max_stamp_cost = None
        stamps.find_link_key = real
    assert closed == ["stamp"] and ol._stamp_tried > 0       # attempted, then failed on its own timeout


def test_f3_lxmf_does_not_retry_a_stamp_failure():
    r, peer = TL._router_with_peer()
    old_link, old_dest = TL.link_mod.OutgoingLink, lxmf.Destination
    TL.link_mod.OutgoingLink = TL.StubOutgoingLink
    lxmf.Destination = TL.FakeDest
    TL.StubOutgoingLink.instances = []
    try:
        msg = r.send_message(peer.hash, "x", desired_method=lxmf.LXMessage.DIRECT)
        first = TL.StubOutgoingLink.instances[0]
        first.close_reason = "stamp"
        first.fail_establishment()
        assert len(TL.StubOutgoingLink.instances) == 1, "stamp failure was retried"
        assert msg.state == lxmf.LXMessage.FAILED
        # an ordinary establishment failure still retries (after the reference
        # retry wait, not at once)
        real_time = time.time
        clock = [real_time()]
        time.time = lambda: clock[0]
        try:
            msg2 = r.send_message(peer.hash, "y", desired_method=lxmf.LXMessage.DIRECT)
            TL.StubOutgoingLink.instances[1].fail_establishment()
            r.process_outbound()
            assert len(TL.StubOutgoingLink.instances) == 2
            clock[0] += 12
            r.process_outbound()
            assert len(TL.StubOutgoingLink.instances) == 3
        finally:
            time.time = real_time
    finally:
        TL.link_mod.OutgoingLink, lxmf.Destination = old_link, old_dest
        lxmf.Identity.known.clear()
        Transport.reachable_destinations.clear()
        Transport._path_waiters.clear()


def _forged_cost_raw(raw, new_fp):
    ext = bytearray(raw[-18:])
    ext[16] = new_fp & 0xFF
    ext[17] = (new_fp >> 8) & 0x7F
    return raw[:-18] + bytes(ext)


def test_f3_known_key_fast_path_does_not_accept_a_changed_link_cost():
    raw, dest_hex, app_hex, lr, _v = TS.VECTORS["v1"]
    dest = bytes.fromhex(dest_hex)
    TS._fresh()
    RI = TS.RealIdentity
    assert RI.validate_announce(TS._unpack(raw)) is True
    stored = RI.recall_link_cost(dest)
    assert stored == 12.5
    # same announce again: fast path (signature skipped), cost unchanged
    assert RI.validate_announce(TS._unpack(raw)) is True
    # forged/replayed announce with a different cost: the signature no longer
    # matches, so it must NOT be accepted on the fast path
    forged = bytes.fromhex(raw)
    forged = _forged_cost_raw(forged, 100 * 256)
    assert RI.validate_announce(TS._unpack(forged.hex())) is False
    assert RI.recall_link_cost(dest) == 12.5
    TS._fresh()


# ---------------------------------------------------------------- F4 ----
LX = lxmf.LXMessage


def _pure_hashcash():
    saved = (stamps._hcmod, stamps._hc_state)
    stamps._hcmod, stamps._hc_state = None, -1
    return saved


def test_f4_pure_python_cap_is_lower_than_native():
    saved = _pure_hashcash()
    try:
        assert LX.pow_stamp_cap() == LX.MAX_POW_STAMP_COST_PY <= 10
        dst, src = TL._pair()
        m = LX(destination=dst, source=src, content=b"hi", title=b"")
        m.stamp_cost = LX.MAX_POW_STAMP_COST_PY + 1
        t0 = time.time()
        m.pack()
        assert m.stamp is None and time.time() - t0 < 0.1          # unstamped, no search
        class Nat:
            def hashcash(self, *a):
                return None, 0
        stamps._hcmod, stamps._hc_state = Nat(), 1
        assert LX.pow_stamp_cap() == 16
    finally:
        stamps._hcmod, stamps._hc_state = saved


def test_f4_expensive_stamp_is_deferred_not_searched_inside_pack():
    saved = _pure_hashcash()
    real = stamps.find_hashcash
    stamps.find_hashcash = lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched in pack"))
    try:
        dst, src = TL._pair()
        m = LX(destination=dst, source=src, content=b"hi", title=b"")
        m.stamp_cost = 8
        assert m.pack(defer_pow=True) is True
        assert len(umsgpack.unpackb(m.packed[32 + 64:])) == 4          # valid unstamped packet meanwhile
        # trivially small work stays inline
        m2 = LX(destination=dst, source=src, content=b"hi", title=b"")
        m2.stamp_cost = 3
        stamps.find_hashcash = real
        assert m2.pack(defer_pow=True) is False and m2.stamp is not None
        # a ticket always wins and needs no search
        stamps.find_hashcash = lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched"))
        m3 = LX(destination=dst, source=src, content=b"hi", title=b"")
        m3.outbound_ticket = bytes(range(16))
        m3.stamp_cost = 8
        assert m3.pack(defer_pow=True) is False
        assert m3.stamp == hashlib.sha256(bytes(range(16)) + m3.message_id).digest()[:16]
    finally:
        stamps.find_hashcash = real
        stamps._hcmod, stamps._hc_state = saved


def test_f4_send_message_searches_in_slices_and_sends_when_ready():
    saved = _pure_hashcash()
    r, peer = TL._router_with_peer()
    r.peers[peer.hash] = {"stamp_cost": 8}
    old_link, old_dest = TL.link_mod.OutgoingLink, lxmf.Destination
    TL.link_mod.OutgoingLink = TL.StubOutgoingLink
    lxmf.Destination = TL.FakeDest
    TL.StubOutgoingLink.instances = []
    gaps = []

    async def main():
        async def beat():
            last = time.time()
            while True:
                await asyncio.sleep(0.003)
                gaps.append(time.time() - last)
                last = time.time()
        b = asyncio.create_task(beat())
        res = r.send_message(peer.hash, "stamped", desired_method=LX.DIRECT)
        assert res is not None and res._pow_pending and TL.StubOutgoingLink.instances == [], "must be queued, nothing sent yet"
        for _ in range(500):
            await asyncio.sleep(0.01)
            if TL.StubOutgoingLink.instances:
                break
        b.cancel()
    try:
        asyncio.run(main())
        link_ = TL.StubOutgoingLink.instances[0]
        link_.establish()
        packed = [c[1] for c in link_.calls if c[0] == "send"][0]
        payload = umsgpack.unpackb(packed[32 + 64:])
        assert len(payload) == 5 and len(payload[4]) == 32
        mid = hashlib.sha256(packed[:32] + umsgpack.packb(payload[:4])).digest()
        assert hashlib.sha256(mid + payload[4]).digest() <= TL._pow_target(8)
        assert not stamps.interactive_active()
        assert max(gaps) < 0.1, max(gaps)
    finally:
        TL.link_mod.OutgoingLink, lxmf.Destination = old_link, old_dest
        lxmf.Identity.known.clear()
        Transport.reachable_destinations.clear()
        stamps._hcmod, stamps._hc_state = saved


def test_f4_send_message_without_event_loop_stamps_inline():
    saved = _pure_hashcash()
    r, peer = TL._router_with_peer()
    r.peers[peer.hash] = {"stamp_cost": 8}
    old_link, old_dest = TL.link_mod.OutgoingLink, lxmf.Destination
    TL.link_mod.OutgoingLink = TL.StubOutgoingLink
    lxmf.Destination = TL.FakeDest
    try:
        msg = r.send_message(peer.hash, "stamped", desired_method=LX.DIRECT)
        assert msg is not True and msg.stamp is not None and len(msg.stamp) == 32
        assert hashlib.sha256(msg.message_id + msg.stamp).digest() <= TL._pow_target(8)
    finally:
        TL.link_mod.OutgoingLink, lxmf.Destination = old_link, old_dest
        lxmf.Identity.known.clear()
        Transport.reachable_destinations.clear()
        stamps._hcmod, stamps._hc_state = saved


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
