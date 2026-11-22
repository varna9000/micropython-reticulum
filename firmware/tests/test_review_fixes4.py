# Fourth code-review round, host-side. Integer-second clock where timing matters
# (IntClock / _IntClockEnv from test_review_fixes2).  Run:
#   python3 firmware/tests/test_review_fixes4.py
import importlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
from harness import const, Transport, reset_transport
import test_lxmf_outbound as TO
from test_lxmf_outbound import Env, FakeLink, FakeResource, _with_env
import test_review_fixes2 as T2

lxmf = TO.lxmf
link_mod = TO.link_mod
resource_mod = TO.resource_mod
LXMessage = TO.LXMessage
RealRes = resource_mod.Resource          # captured before any Env swaps in FakeResource


# ---------------------------------------------------------------- F3 ----
def _outgoing_resource(status):
    ol = object.__new__(T2.RealOL)
    ol.pending_requests = {}
    res = object.__new__(RealRes)
    res.status = status
    res.hash = b"\x09" * 32
    res.link = ol
    res.request_id = None
    ol.outgoing_resources = [res]
    ol.incoming_resources = []
    ol.torn = []
    ol.teardown = lambda: ol.torn.append(1)
    return ol, res


@_with_env()
def test_f3_receiver_cancel_mid_transfer_is_retried_not_rejected(e):
    msg = e.send(TO._big(), desired_method=LXMessage.DIRECT)
    ol, res = _outgoing_resource(resource_mod.TRANSFERRING)    # parts were requested already
    msg._delivery_link = ol
    seen = []
    ol.resource_concluded_callback = lambda r: (seen.append(r.status), msg._resource_concluded(r))
    ol._handle_resource_cancel(res.hash, True)                 # RCL from the receiver (stall)
    assert seen == [resource_mod.FAILED], seen
    assert msg.state == LXMessage.OUTBOUND and ol.torn == [1]  # normal retry path, link dropped


@_with_env()
def test_f3_receiver_cancel_before_any_part_is_a_final_rejection(e):
    msg = e.send(TO._big(), desired_method=LXMessage.DIRECT)
    ol, res = _outgoing_resource(resource_mod.ADVERTISED)      # advertisement refused
    msg._delivery_link = ol
    seen = []
    ol.resource_concluded_callback = lambda r: (seen.append(r.status), msg._resource_concluded(r))
    ol._handle_resource_cancel(res.hash, True)
    assert seen == [resource_mod.REJECTED] and msg.state == LXMessage.REJECTED


# ---------------------------------------------------------------- F5 ----
import asyncio
import shutil
import announce_stamp_support as S
A = S.announce_stamps


def _f5_run(announce_first, stored_first=False):
    d = S.make_destination()
    tmp = TA._tmp()
    A.configure(enabled=True, value=12, allow_low=True, duty=50)
    S.Transport.destinations = [d]
    ifc = harness.MockInterface("up")
    S.Transport.interfaces = [ifc]
    S.Transport.announce_inline = True
    old_sp = S.RealIdentity.storagepath
    S.RealIdentity.storagepath = tmp
    S.RealDestination.stamp_announces = True
    try:
        if stored_first:
            TA._install(tmp, d)
        async def main():
            if announce_first:
                assert d.announce(app_data=b"x") is True      # goes out (stamped when a stamp is stored)
            else:
                A.request(d)                                    # generation only, never announced
            t0 = time.time()
            while d.hash in A._tasks and time.time() - t0 < 60:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)                            # nothing more may follow
        asyncio.run(main())
        return len(ifc.sent), [len(Transport_unpack(raw).data) for raw in ifc.sent]
    finally:
        S.RealDestination.stamp_announces = False
        S.RealIdentity.storagepath = old_sp
        A.reset()
        shutil.rmtree(tmp, ignore_errors=True)


def Transport_unpack(raw):
    return TA._unpack(raw)


TA = importlib.import_module("test_announce_stamps")


def test_f5_one_announce_when_a_stamp_becomes_available_after_an_unstamped_announce():
    n, lens = _f5_run(announce_first=True)
    assert n == 2, (n, lens)                                    # the unstamped one + exactly one re-announce
    assert lens[1] == 148 + 18, lens                          # default app_data (none here) + the 18-byte stamp extension


def test_f5_no_extra_announce_when_nothing_was_announced_or_already_stamped():
    n, lens = _f5_run(announce_first=False)
    assert n == 0, (n, lens)                                    # never announced: nothing to repair
    n, lens = _f5_run(announce_first=True, stored_first=True)
    assert n == 1, (n, lens)                                    # went out stamped already


# ---------------------------------------------------------------- F6 ----
def _second_send(e, router_slow, observed=None, send_twice=False):
    """Seconds (on the integer clock) at which attempt 2 of a fresh opportunistic message goes out."""
    ic = T2._int_clock(e)
    try:
        e.router._slow_crypto = router_slow
        if observed is not None:
            e.router.note_proof_latency(e.peer.hash, observed)
        msg = e.send("hello", desired_method=LXMessage.OPPORTUNISTIC)
        e.advance_to(80)
        ts = e.sent_times()
        assert len(ts) >= 2, ts
        return ts[1]
    finally:
        ic()


@_with_env()
def test_f6_native_board_keeps_the_reference_wait(e):
    t = _second_send(e, router_slow=False)
    assert 10 <= t <= 14, t                      # 10 s wait, next 4 s job tick


@_with_env()
def test_f6_board_without_native_crypto_starts_from_a_longer_first_wait(e):
    t = _second_send(e, router_slow=True)
    assert 25 <= t <= 29, t                      # 10 s + the 15 s receipt crypto grace


@_with_env()
def test_f6_observed_proof_latency_sets_the_wait_per_peer(e):
    t = _second_send(e, router_slow=False, observed=20)
    assert 30 <= t <= 34, t                      # 1.5 x 20 s
    e.router._proof_latency.clear()


@_with_env()
def test_f6_short_observed_latency_never_goes_below_the_reference_wait(e):
    t = _second_send(e, router_slow=True, observed=2)
    assert 10 <= t <= 14, t                      # the observation replaces the no-native default; floor stays 10 s


@_with_env()
def test_f6_latency_is_measured_from_the_receipt_of_the_proved_attempt_on_integer_ticks(e):
    ic = T2._int_clock(e)
    try:
        e.router._slow_crypto = True               # first wait 25 s: no retry before the proof at 20 s
        msg = e.send("hello", desired_method=LXMessage.OPPORTUNISTIC)
        e.advance_to(20)
        assert len(e.sent_times()) == 1
        receipt = msg._receipts[-1]
        msg._mark_delivered(receipt)
        assert e.router._proof_latency[e.peer.hash] == 20.0, e.router._proof_latency
        for i in range(40):                         # bounded cache
            e.router.note_proof_latency(bytes([i]) * 16, 1.0)
        assert len(e.router._proof_latency) <= e.router._LATENCY_MAX
    finally:
        ic()


# ---------------------------------------------------------------- F7 ----
class _KwLink(FakeLink):
    seen = []

    def __init__(self, destination, established_callback=None, closed_callback=None, **kw):
        _KwLink.seen.append(kw)
        super().__init__(destination, established_callback, closed_callback, **kw)


def _sign_proofs_flag(e, native):
    link_mod.OutgoingLink = _KwLink
    _KwLink.seen = []
    real = lxmf.ed25519.have_native
    lxmf.ed25519.have_native = lambda: native
    try:
        e.send(TO._big(), desired_method=LXMessage.DIRECT)
    finally:
        lxmf.ed25519.have_native = real
    assert len(_KwLink.seen) == 1
    return _KwLink.seen[0]["sign_proofs"]


@_with_env()
def test_f7_link_proofs_are_signed_only_with_native_ed25519(e):
    assert _sign_proofs_flag(e, True) is True


@_with_env()
def test_f7_no_blocking_key_generation_without_native_ed25519(e):
    assert _sign_proofs_flag(e, False) is False


# ---------------------------------------------------------------- F8 ----
import asyncio
import importlib
transport_mod = importlib.import_module("urns.transport")


def _run_job_loop(validate_ms, queue_len, run_ms=60_000, tick_validate_ms=None):
    """Drive the REAL Transport.job_loop on a virtual millisecond clock. Time advances only
    when the loop awaits sleep_ms (other tasks may run: that time is "free") or when a
    validation blocks (simulated cost of one announce). Returns (blocked_ms, free_ms, events)."""
    vt = [0]
    events = []
    free = [0]
    blocked = [0]
    real = (time.time, const.ticks_ms, asyncio.sleep_ms, Transport._handle_announce,
            const.ticks_diff)
    time.time = lambda: vt[0] // 1000                        # integer-second wall clock
    const.ticks_ms = lambda: vt[0]
    const.ticks_diff = lambda a, b: a - b

    async def sleep_ms(ms):
        events.append("sleep")
        vt[0] += ms
        free[0] += ms
        if vt[0] >= run_ms:
            Transport._jobs_running = False
        await asyncio.sleep(0)

    def handle(packet):
        events.append("validate")
        vt[0] += validate_ms
        blocked[0] += validate_ms

    asyncio.sleep_ms = sleep_ms
    Transport._handle_announce = staticmethod(handle)
    reset_transport()
    Transport.announce_inline = False
    Transport.announce_ingress = [types_ns() for _ in range(queue_len)]
    Transport._jobs_running = True
    try:
        asyncio.run(Transport.job_loop())
    finally:
        (time.time, const.ticks_ms, asyncio.sleep_ms, Transport._handle_announce,
         const.ticks_diff) = real
        Transport._jobs_running = False
        Transport.announce_inline = True
        Transport.announce_ingress = []
    return blocked[0], free[0], events


def types_ns():
    import types
    return types.SimpleNamespace(destination_hash=b"\x01" * 16)


def test_f8_slow_validation_leaves_other_tasks_at_least_about_half_the_time():
    blocked, free, events = _run_job_loop(validate_ms=2000, queue_len=32)
    share = free / (free + blocked)
    assert share >= 0.45, (share, blocked, free)
    for a, b in zip(events, events[1:]):
        assert not (a == "validate" and b == "validate"), "two drain slices without an await between"


def test_f8_fast_validation_still_drains_and_never_runs_two_slices_back_to_back():
    blocked, free, events = _run_job_loop(validate_ms=5, queue_len=32, run_ms=5000)
    assert events.count("validate") == 32                    # the whole burst is drained within the run
    # a slice is one or more validations; between slices an await is mandatory
    slices = 0
    prev = None
    for ev in events:
        if ev == "validate" and prev != "validate":
            slices += 1
        prev = ev
    assert events.count("sleep") >= slices


# ---------------------------------------------------------------- run ----
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
