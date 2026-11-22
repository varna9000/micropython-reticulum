# Second code-review round (ten findings), host-side. Run:
#   python3 firmware/tests/test_review_fixes2.py
#
# One section per finding (R1..R10). MicroPython's time.time() returns INTEGER
# seconds, so every test that touches timing runs with an integer wall clock
# (time.time patched to int) and a separate millisecond tick source
# (const.ticks_ms patched) -- never with CPython's float clock.

import asyncio
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
from harness import const, Transport, MockInterface, Identity, reset_transport
from harness import build_announce_hdr1, build_announce_data
import test_lxmf_outbound as TO
from test_lxmf_outbound import Env, FakeLink, _with_env
import test_announce_ingress as TI
import test_announce_stamps as TA
import announce_stamp_support as S

lxmf = TO.lxmf
link_mod = TO.link_mod
packet_mod = TO.packet_mod
resource_mod = TO.resource_mod
LXMessage = TO.LXMessage
LXMRouter = TO.LXMRouter
RealOL = link_mod.OutgoingLink          # captured before any Env swaps in FakeLink
stamps = importlib.import_module("urns.stamps")
umsgpack = importlib.import_module("urns.umsgpack")
A = S.announce_stamps
FW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _IntClockEnv:
    """Integer-second wall clock + ms tick source, both driven by Env's float
    time e.t[0] (seconds). jump(delta) moves ONLY the wall clock (a clock sync);
    the tick source keeps counting real elapsed time. Call as restore()."""

    def __init__(self, e):
        self.e = e
        self.off = 0.0
        self.real = (time.time, const.ticks_ms)
        time.time = lambda: int(e.t[0])
        const.ticks_ms = lambda: int(round((e.t[0] - self.off) * 1000))

    def jump(self, delta):
        self.e.t[0] += delta
        self.off += delta

    def __call__(self):
        time.time, const.ticks_ms = self.real


def _int_clock(e):
    return _IntClockEnv(e)


class IntClock:
    """Stand-alone integer clock for tests without an Env: .ms drives both."""

    def __init__(self, start_ms):
        self.ms = start_ms
        self.real = (time.time, const.ticks_ms)
        time.time = lambda: self.ms // 1000
        const.ticks_ms = lambda: self.ms

    def close(self):
        time.time, const.ticks_ms = self.real


def _lxmf_bytes(content=b"hi", source=b"S" * 16):
    payload = umsgpack.packb([1700000000.0, b"", content, {}])
    return b"D" * 16 + source + b"\x00" * 64 + payload


# ---------------------------------------------------------------- R1 ----
class ProvingLink(FakeLink):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.proved = []
        self.packet_callback = None

    def prove_packet(self, packet):
        self.proved.append(packet)


@_with_env()
def test_r1_direct_link_is_closed_when_delivery_is_done_and_shared_until_then(e):
    e.send("one", desired_method=LXMessage.DIRECT)
    e.send("two", desired_method=LXMessage.DIRECT)
    m1, m2 = e.router.pending_outbound
    assert len(FakeLink.instances) == 1
    link = FakeLink.instances[0]
    link.establish()
    link.receipts[0].delivery_callback(None)
    assert m1.state == LXMessage.DELIVERED and link.status == 0x01      # m2 still needs it
    link.receipts[1].delivery_callback(None)
    assert m2.state == LXMessage.DELIVERED
    assert link.status == 0x02 and ("teardown", None) in link.calls     # closed promptly
    assert e.peer.hash not in e.router.direct_links
    assert not hasattr(LXMRouter, "LINK_MAX_INACTIVITY"), "no idle-hold timer any more"


@_with_env()
def test_r1_inbound_lxmf_on_our_own_link_is_delivered_and_proved(e):
    link_mod.OutgoingLink = ProvingLink
    got = []
    e.router.register_delivery_callback(lambda m: got.append(m))
    e.send("x", desired_method=LXMessage.DIRECT)
    link = FakeLink.instances[0]
    link.establish()
    assert callable(link.packet_callback)
    pkt = types.SimpleNamespace(packet_hash=b"H" * 32)
    link.packet_callback(_lxmf_bytes(b"reply on our link"), pkt)
    assert len(got) == 1 and got[0].content_as_string() == "reply on our link"
    assert link.proved == [pkt], "the packet must be proved"
    # a Resource the peer sends back over our link goes to the same path
    res = types.SimpleNamespace(is_initiator=False, status=resource_mod.COMPLETE,
                                data=_lxmf_bytes(b"resource reply", b"T" * 16),
                                request_id=None, link=link)
    link.resource_concluded_callback(res)
    assert len(got) == 2 and got[1].content_as_string() == "resource reply"


# ---------------------------------------------------------------- R2/R3 ----
class _PassToken:
    def __init__(self, key=None):
        pass

    def encrypt(self, data):
        return bytes(data)

    def decrypt(self, data):
        return bytes(data)


def _pending_real_link(request_ms):
    """A real OutgoingLink waiting for the proof, with real crypto and a stub
    identity that accepts any signature."""
    crypto = importlib.import_module("urns.crypto")
    crypto.Token = _PassToken           # no AES on the host rig; X25519 stays real
    ol = object.__new__(RealOL)
    ol.status = RealOL.PENDING
    ol.link_id = b"L" * 16
    ol.hash = ol.link_id
    ol.mtu, ol.sdu, ol.mdu = 500, 465, 431
    ol.destination = types.SimpleNamespace(
        hash=TO.FakeIdentity(b"peer").hash, hexhash="aa",
        identity=types.SimpleNamespace(validate=lambda s, m: True, sig_pub_bytes=b"\x07" * 32))
    ol.pending_requests = {}
    ol.incoming_resources, ol.outgoing_resources = [], []
    ol.established_callback = ol.closed_callback = ol.packet_callback = None
    ol.resource_concluded_callback = ol.resource_started_callback = None
    ol.remote_identified_callback = None
    ol.remote_identity = None
    ol._channel = None
    ol.rtt = 0
    ol.last_activity = ol.last_outbound = ol.request_time = time.time()
    ol._request_ms = request_ms
    ol._last_keepalive = time.time()
    ol._prv = crypto.X25519PrivateKey.generate()
    peer_pub = crypto.X25519PrivateKey.generate().public_key().public_bytes()
    proof = types.SimpleNamespace(data=b"\x00" * 64 + peer_pub)
    Transport.pending_links.append(ol)
    return ol, proof


@_with_env()
def test_r2_link_rtt_is_measured_in_milliseconds_with_an_integer_clock(e):
    link_mod.OutgoingLink = RealOL            # the real class is under test (Env swapped in FakeLink)
    restore = _int_clock(e)
    try:
        Transport.interfaces = [MockInterface("m")]
        e.t[0] = 1_000_010.300
        ol, proof = _pending_real_link(const.ticks_ms())
        e.t[0] = 1_000_011.900                  # 1.6 s later; whole seconds say 1 (<1.45 s regime)
        ol.validate_proof(proof)
        assert isinstance(ol.rtt, float) and 1.55 < ol.rtt < 1.65, ol.rtt
        assert ol._keepalive_interval() == 300, "rtt 1.6 s is the slow regime: no 5 s keepalive pair"
        # a sub-second rtt is a real fraction, not 0 (falsy) or 1
        ol2, proof2 = _pending_real_link(const.ticks_ms())
        e.t[0] += 0.350
        ol2.validate_proof(proof2)
        assert 0.3 < ol2.rtt < 0.4, ol2.rtt
    finally:
        restore()


@_with_env()
def test_r3_link_packet_proof_300_to_900_ms_after_send_marks_delivered_without_resend(e):
    link_mod.OutgoingLink = RealOL
    restore = _int_clock(e)
    try:
        Transport.interfaces = [MockInterface("m")]
        mi = Transport.interfaces[0]
        e.t[0] = 1_000_010.100
        ol, proof = _pending_real_link(const.ticks_ms())
        e.t[0] = 1_000_010.150                  # rtt 0.05 s: same wall second (old code: rtt 0)
        ol.validate_proof(proof)
        assert ol.rtt < 0.1
        e.router.direct_links[e.peer.hash] = ol
        e.t[0] = 1_000_010.500                  # wall second 10
        msg = e.send("y" * 290, desired_method=LXMessage.DIRECT)
        assert msg.method == LXMessage.DIRECT and msg.representation == LXMessage.PACKET, \
            "290 B of content must go as ONE link packet"
        assert msg.state == LXMessage.SENDING and len(Transport.receipts) == 1
        receipt = Transport.receipts[0]
        n_sent = len(mi.sent)
        # Transport.job_loop's receipt sweep at +0.5 s (wall second 11) and at +0.9 s
        for t in (1_000_011.000, 1_000_011.400):
            e.t[0] = t
            for r in list(Transport.receipts):
                r.check_timeout()
            assert receipt.status == packet_mod.PacketReceipt.SENT, "timed out before its proof could arrive"
        e.router.process_outbound()
        assert msg.state == LXMessage.SENDING and ol.status == RealOL.ACTIVE, "no teardown"
        assert len(mi.sent) == n_sent, "no resend"
        # the proof arrives 0.9 s after the send
        assert receipt.validate_proof(receipt.hash + b"\x00" * 64) is True
        assert msg.state == LXMessage.DELIVERED and len(e.ev("delivered")) == 1
        assert len(e.ev("sent")) == 1
        assert 0.85 < receipt.get_rtt() < 0.95, receipt.get_rtt()
    finally:
        restore()


def test_r3_receipt_floor_and_tick_based_timeout():
    clk = IntClock(10_500)                       # wall second 10
    try:
        dest = types.SimpleNamespace(type=const.DEST_LINK, hash=b"x" * 16, rtt=0.0)
        pkt = types.SimpleNamespace(destination=dest, get_hash=lambda: b"h" * 32,
                                    getTruncatedHash=lambda: b"h" * 16)
        r = packet_mod.PacketReceipt(pkt)
        assert r.timeout >= const.LINK_RECEIPT_MIN_TIMEOUT >= 2
        clk.ms = 11_400                          # 0.9 s later, next wall second
        assert r.is_timed_out() is False
        clk.ms = 10_500 + int(r.timeout * 1000) + 1
        assert r.is_timed_out() is True
    finally:
        clk.close()


# ---------------------------------------------------------------- R4 ----
@_with_env(with_path=False)
def test_r4_concluded_messages_do_not_count_against_the_queue_bound(e):
    bound = LXMRouter.MAX_PENDING_OUTBOUND
    msgs = [e.send("m%d" % i) for i in range(bound)]
    assert all(m is not None for m in msgs) and e.send("full") is None
    msgs[0]._mark_delivered()                                  # proof arrived, next 4 s tick not yet
    assert msgs[0] not in e.router.pending_outbound, "delivered messages leave the queue promptly"
    assert e.send("fits") is not None
    msgs[1].state = LXMessage.CANCELLED                        # concluded, not yet reaped
    msgs[2].state = LXMessage.FAILED
    assert e.send("fits too") is not None and e.send("and this") is not None
    assert msgs[1] not in e.router.pending_outbound and msgs[2] not in e.router.pending_outbound
    assert len(e.ev("failed")) == 2, "their failed callbacks still fire, once"
    assert len(e.router.pending_outbound) <= bound


# ---------------------------------------------------------------- R5 ----
@_with_env()
def test_r5_clock_jump_while_awaiting_proof_causes_no_resend_or_failure(e):
    restore = _int_clock(e)
    try:
        msg = e.send("hello")
        assert msg.state == LXMessage.SENT and len(e.sent) == 1
        e.router.request_path(e.peer.hash)                     # a recorded path request time
        before = time.time()
        delta = 20 * 365 * 86400
        restore.jump(delta)                                    # the clock syncs
        Transport._shift_clocks(delta, before + const.PATH_EXPIRY + 60)
        for k in range(2):                                     # job ticks right after the sync
            e.t[0] += 4
            e.router.process_outbound()
        assert len(e.sent) == 1, "no extra send"
        assert msg.state == LXMessage.SENT and not e.ev("failed")
        assert msg.next_delivery_attempt is not None and msg.next_delivery_attempt < e.t[0] + 20
        e.t[0] += 8                                            # the normal retry wait (10 s) still applies
        e.router.process_outbound()
        assert len(e.sent) == 2
        # receipt timeouts run on ticks: a real receipt is untouched by the jump
        dest = types.SimpleNamespace(type=const.DEST_LINK, hash=b"x" * 16, rtt=1.0)
        r = packet_mod.PacketReceipt(types.SimpleNamespace(
            destination=dest, get_hash=lambda: b"h" * 32, getTruncatedHash=lambda: b"h" * 16))
        restore.jump(delta)
        assert r.is_timed_out() is False
    finally:
        restore()


def test_r5_pow_stamp_search_budget_survives_a_clock_jump():
    e = Env()
    restore = _int_clock(e)
    calls = []
    real_find = stamps.find_hashcash
    real_duty = stamps.Duty

    class QuickDuty:
        def __init__(self, *a, **k):
            pass

        def begin(self):
            pass

        def rest_ms(self):
            return 0
    jumped = []

    def fake_find(material, target, slen, budget=None):
        calls.append(e.t[0])
        e.t[0] += 1.0                                          # each slice takes 1 s
        if len(calls) == 1 and not jumped:
            jumped.append(1)
            restore.jump(20 * 365 * 86400)                     # wall clock syncs mid-search
        return None, 10
    stamps.find_hashcash, stamps.Duty = fake_find, QuickDuty
    try:
        m = LXMessage(destination=None, source=None, content=b"x", destination_hash=e.peer.hash)
        m.message_id = b"M" * 32
        res = asyncio.run(m._pow_stamp_async(10, max_s=6))
        assert res is None
        assert len(calls) == 6, "budget is 6 s of search time, not wall-clock: %d slices" % len(calls)
    finally:
        stamps.find_hashcash, stamps.Duty = real_find, real_duty
        restore()
        e.close()


# ---------------------------------------------------------------- R6/R8 ----
_BOOT = r'''
import sys, types, hashlib, asyncio, os, json, tempfile, gc
FW, CFG, OUT = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3]
sys.path.insert(0, FW)
mp = types.ModuleType("micropython"); mp.const = lambda x: x; mp.native = lambda f: f; mp.viper = lambda f: f
sys.modules["micropython"] = mp
sys.modules["uhashlib"] = hashlib
if not hasattr(asyncio, "sleep_ms"):
    async def _s(ms): await asyncio.sleep(ms / 1000)
    asyncio.sleep_ms = _s
sys.modules["uasyncio"] = asyncio
sys.modules["machine"] = types.ModuleType("machine")
uc = types.ModuleType("ucryptolib"); uc.aes = None; sys.modules["ucryptolib"] = uc
gc.mem_free = lambda: 100000
gc.threshold = lambda *a: None
d = tempfile.mkdtemp()
import urns
from urns import Reticulum, Destination
cfg = d + "/config.json"
json.dump(dict(CFG, loglevel=0, interfaces=[]), open(cfg, "w"))
r = Reticulum(cfg)
r.setup_interfaces()
dest = Destination(r.identity, Destination.IN, Destination.SINGLE, "test", "node")
dest.announce()
from urns import identity as idm
out = {"stamp_modules": sorted(m for m in sys.modules if m.endswith("stamps")),
       "required": idm.ANNOUNCE_STAMP_COST}
if CFG.get("_probe_split"):
    from urns import stamps
    dh, pk, nh = b"d" * 16, b"p" * 64, b"n" * 10
    tgt = stamps.target_from_fp(stamps.cost_to_fp(CFG["_probe_split"]))
    st = None
    while st is None:
        st, _ = stamps.find_hashcash(dh + pk + nh, tgt, 14)
    ext = bytes((0xC1, 0x00)) + st + b"\x00\x00"
    app, fp = idm._split_ext(dh, pk, nh, b"app" + ext)
    out["split_accepted"] = (app == b"app" and fp == 0)
json.dump(out, open(OUT, "w"))
'''


def _boot(cfg):
    out = tempfile.mktemp(suffix=".json")
    script = tempfile.mktemp(suffix=".py")
    open(script, "w").write(_BOOT)
    p = subprocess.run([sys.executable, "-I", script, FW, json.dumps(cfg), out],
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.load(open(out))


def test_r6_boot_and_announce_import_no_stamp_module_when_stamping_is_off():
    res = _boot({})
    assert res["stamp_modules"] == [], res
    # explicit opt-in still works (module loads, no native needed)
    res = _boot({"stamp_announces": True, "announce_stamp_value": 4, "announce_stamp_allow_low": True})
    assert any(m.endswith("announce_stamps") for m in res["stamp_modules"]), res


def test_r8_inbound_announce_stamp_cost_is_configurable():
    low = _boot({"announce_stamp_required": 9, "_probe_split": 10})
    assert low["required"] == 9.0 and low["split_accepted"] is True
    dflt = _boot({"_probe_split": 10})
    assert dflt["required"] == 23.5 and dflt["split_accepted"] is False


# ---------------------------------------------------------------- R7 ----
def test_r7_stored_stamp_below_configured_value_is_used_then_replaced():
    d = S.make_destination()
    tmp = TA._tmp()
    saved_min = A.MIN_COST
    A.MIN_COST = 6.0                                   # make a cheap stamp acceptable to the hub
    try:
        A.configure(enabled=True, value=14, allow_low=True, duty=50)
        mat = A.material(d)
        # a cost-7 search lands on a value >= 14 about once in 128 tries; redraw
        # so the "stored stamp is below the configured value" premise always holds
        while True:
            old, _ = stamps.find_hashcash(mat, stamps.target_from_fp(stamps.cost_to_fp(7)), 14, 100000)
            if old is not None and stamps.stamp_value(mat, old) < 14:
                break
        os.makedirs(tmp + "/stamps", exist_ok=True)
        A.store(d.hash, old, stamps.stamp_value(mat, old))
        A._cache.clear()
        A.MIN = 6.0

        async def main():
            ext = A.for_announce(d)
            assert ext is not None and ext[2:16] == old, "the lower stamp is used meanwhile"
            t0 = time.time()
            while A._tasks or A._pending:
                assert time.time() - t0 < 60
                await asyncio.sleep(0.01)
        asyncio.run(main())
        new = A.get_stamp(d)
        assert new != old and stamps.stamp_valid(new, 14, mat), "replaced by a stamp at the configured value"
        assert A.for_announce(d)[2:16] == new
    finally:
        A.MIN_COST = saved_min
        A.reset()


# ---------------------------------------------------------------- R9 ----
def _search_link(budget=120, cost=10.0):
    ol = object.__new__(RealOL)
    ol.status = RealOL.PENDING
    ol.link_id = ol.hash = b"\x00" * 16
    ol.destination = types.SimpleNamespace(hash=b"D" * 16, hexhash="dd")
    ol._sig_pub_bytes = os.urandom(32)
    ol._stamp_cost = cost
    ol._stamp_tried = 0
    ol._stamp_deadline = True
    ol._stamp_budget_ms = int(budget * 1000)
    ol._stamp_spent_ms = 0
    ol._stamp_waiting = False
    ol._stamp_start_ms = ol._stamp_mark = const.ticks_ms()
    ol._stamp_hard_ms = ol._stamp_budget_ms * RealOL.STAMP_QUEUE_FACTOR
    ol._stamp_material = b"D" * 16 + ol._sig_pub_bytes
    ol.pending_requests = {}
    ol.incoming_resources, ol.outgoing_resources = [], []
    ol.established_callback = ol.closed_callback = ol.packet_callback = None
    ol.resource_concluded_callback = ol.resource_started_callback = None
    ol._channel = None
    ol.sent_request = False

    def _send_request():
        ol.sent_request = True
        ol._stamp_deadline = None
    ol._send_request = _send_request
    return ol


def test_r9_pure_python_key_searches_take_turns_instead_of_evicting_each_other():
    stamps.reset_search()
    constructed = []
    real_cls = stamps._PySearch

    class Counting(real_cls):
        def __init__(self):
            constructed.append(1)
            super().__init__()
    stamps._PySearch = Counting
    real_native = stamps._native
    stamps._native = lambda: None                        # force the pure-Python search
    real_slice = stamps.slice_size
    stamps.slice_size = lambda: 32
    a, b = _search_link(), _search_link()
    try:
        for _ in range(6000):
            for ol in (a, b):
                if ol.status == RealOL.PENDING and not ol.sent_request:
                    ol._stamp_step(b"D" * 16)
            if a.sent_request and b.sent_request:
                break
        assert a.sent_request and b.sent_request, "both searches must finish"
        assert len(constructed) == 2, "one resumable search per link, no restarts: %d" % len(constructed)
    finally:
        stamps._PySearch, stamps._native, stamps.slice_size = real_cls, real_native, real_slice
        stamps.reset_search()
        del stamps._py_queue[:]


def test_r9_closing_a_queued_link_keeps_the_running_searchs_state_and_waiting_is_free():
    clk = IntClock(5_000_000)
    stamps.reset_search()
    real_native = stamps._native
    stamps._native = lambda: None
    real_slice = stamps.slice_size
    stamps.slice_size = lambda: 32
    # cost 40 cannot be met in a few 32-candidate slices: at cost 10 the search
    # succeeded by chance in about 1 run in 20 and the test then failed spuriously
    a, b = _search_link(budget=10, cost=40.0), _search_link(budget=10, cost=40.0)
    try:
        a._stamp_step(b"D" * 16)
        state = stamps._py
        assert state is not None and not a.sent_request
        clk.ms += 25_000                                  # b queues for 25 s: longer than its 10 s budget
        b._stamp_step(b"D" * 16)
        assert b.status == RealOL.PENDING, "waiting for another search's turn must not burn the budget"
        assert b._stamp_spent_ms == 0, "queue time is not charged to the budget"
        assert stamps._py is state, "a queued link must not evict the running search"
        b._stamp_failed("cancelled")                      # b closes: only ITS state may be dropped
        assert stamps._py is state, "closing b reset a's search"
        # a hard bound on wait + search still exists
        c = _search_link(budget=10, cost=40.0)
        c._stamp_step(b"D" * 16)
        clk.ms += 41_000
        c._stamp_step(b"D" * 16)
        assert c.status == RealOL.CLOSED and c.close_reason == "stamp"
    finally:
        stamps._native, stamps.slice_size = real_native, real_slice
        stamps.reset_search()
        del stamps._py_queue[:]
        clk.close()


# ---------------------------------------------------------------- R10 ----
def test_r10_older_emission_from_a_known_peer_is_validated_but_does_not_extend_the_route():
    reset_transport()
    iface = MockInterface("tcp")
    Transport.interfaces = [iface]
    dest = b"\xDA" * 16
    entry = TI._install_known(dest, hops_raw=0, emitted=1000, iface=iface)
    entry[const.IDX_PT_EXPIRES] = 5000.0
    entry[const.IDX_PT_TIMESTAMP] = 4000.0
    calls = []
    real = Identity.validate_announce

    def rec(packet_, only_validate_signature=False):
        calls.append(Transport._announce_emitted(packet_))
        return True
    Identity.validate_announce = staticmethod(rec)
    Transport.announce_inline = False
    try:
        # the very announce we already processed (same random blob): a true duplicate, skipped
        Transport.inbound(build_announce_hdr1(dest, data=build_announce_data(emitted=1000)), iface)
        assert Transport.announce_ingress == [] and calls == []
        assert entry[const.IDX_PT_EXPIRES] > 1e9, "a true duplicate still re-confirms the route"
        entry[const.IDX_PT_EXPIRES] = 5000.0
        # a different announce from a peer whose clock stepped back: must be validated
        Transport.inbound(build_announce_hdr1(dest, data=build_announce_data(emitted=900)), iface)
        assert len(Transport.announce_ingress) == 1, "an older emission is not a duplicate"
        Transport._service_announce_ingress()
        assert calls == [900]
        assert entry[const.IDX_PT_EXPIRES] == 5000.0, "an old emission must not extend the path's lifetime"
        assert entry[const.IDX_PT_EMITTED] == 1000
        # and now that one is the duplicate
        Transport.inbound(build_announce_hdr1(dest, data=build_announce_data(emitted=900)), iface)
        assert Transport.announce_ingress == []
        # bounded
        for i in range(const.MAX_PATH_TABLE * 6):
            Transport._note_blob(i.to_bytes(2, "big") + b"\x00" * 14, b"b" * 10)
        assert len(Transport._seen_blob) <= const.MAX_PATH_TABLE * 2
    finally:
        Identity.validate_announce = real
        Transport.announce_inline = True


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
