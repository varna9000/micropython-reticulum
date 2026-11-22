# Third code-review round (ten findings), host-side. Run:
#   python3 firmware/tests/test_review_fixes3.py
#
# One section per finding (X1..X10). Tests drive the REAL code path: the boot path of
# Reticulum.setup_interfaces in a subprocess with a real storage dir, real OutgoingLink /
# Link objects wired back-to-back, integer-second clocks (MicroPython time.time()).

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
FW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(os.path.dirname(FW), "tools", "gen_announce_stamp.py")

# ---------------------------------------------------------------- X1 ----
# Phase "identity": boot a real Reticulum on a real storage dir, print the stamp material of
# a lxmf.delivery destination. Phase "announce": boot again on the same dir (as a board does
# after an upload), setup_interfaces with the given config, announce, report what happened.
_BOOT = r'''
import sys, types, hashlib, asyncio, os, json, gc
FW, PHASE, STORE, CFG, OUT = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4]), sys.argv[5]
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
import urns
from urns import Reticulum, Destination
cfg = STORE + "/config.json"
json.dump(dict(CFG, loglevel=0, interfaces=[]), open(cfg, "w"))
r = Reticulum(cfg)
r.setup_interfaces()
dest = Destination(r.identity, Destination.IN, Destination.SINGLE, "lxmf", "delivery")
out = {"dest": dest.hexhash,
       "material": (dest.hash + r.identity.get_public_key() + dest.name_hash).hex()}
if PHASE == "announce":
    from urns import identity as idm
    p = dest.announce(app_data=b"app", send=False)
    p.pack()
    out["ext"] = p.data[-18:].hex() if p.data[-18] == 0xC1 else None
    out["stamp"] = p.data[-16:-2].hex()
    out["stamp_modules"] = sorted(m for m in sys.modules if m.endswith("stamps"))
    # receive side: validate that stamped announce as a peer would (key not known yet)
    from urns.packet import Packet
    from urns.identity import Identity as _I
    rx = Packet(None, p.raw)
    assert rx.unpack()
    _I.known_destinations.clear()
    out["rx_valid"] = bool(_I.validate_announce(rx))
    out["rx_app_data"] = (_I.recall_app_data(dest.hash) or b"").hex()
    out["rx_modules"] = sorted(m.rsplit(".", 1)[-1] for m in sys.modules if "stamp" in m)
json.dump(out, open(OUT, "w"))
'''


def _boot(phase, store, cfg):
    out = tempfile.mktemp(suffix=".json")
    script = tempfile.mktemp(suffix=".py")
    open(script, "w").write(_BOOT)
    p = subprocess.run([sys.executable, "-I", script, FW, phase, store, json.dumps(cfg), out],
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.load(open(out))


def test_x1_uploaded_stamp_is_attached_on_the_real_boot_path_without_native_hashcash():
    store = tempfile.mkdtemp()
    cfg = {"announce_stamp_value": 10, "announce_stamp_allow_low": True,
           "announce_stamp_required": 10}               # stamp_announces stays at the
    # default: off here, because the host has no native hashcash (asserted by the control below)
    first = _boot("identity", store, cfg)
    # control: no stamp file -> unstamped, and no stamp module was imported
    res = _boot("announce", store, cfg)
    assert res["ext"] is None and res["stamp_modules"] == [], res
    # the operator's tool writes the file; its --out is <storage>/stamps as its upload hint says
    p = subprocess.run([sys.executable, "-I", TOOL, "--material", first["material"], "--value", "10",
                        "--out", store + "/stamps", "--procs", "1", "--storage", store],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert os.path.exists(store + "/stamps/" + first["dest"] + ".dstamp")
    res = _boot("announce", store, cfg)
    assert res["ext"] is not None and res["ext"].startswith("c100"), res
    assert any(m.endswith("announce_stamps") for m in res["stamp_modules"]), res
    st = importlib.import_module("urns.stamps")
    assert st.stamp_valid(bytes.fromhex(res["stamp"]), 10.0, bytes.fromhex(first["material"]))
    # finding 9: receiving a stamped announce loads the small validation module, not the search code
    assert res["rx_valid"] is True and res["rx_app_data"] == b"app".hex(), res
    assert "stamp_check" in res["rx_modules"], res
    assert "stamps" not in res["rx_modules"], "the search module must not be imported by validation: %r" % res["rx_modules"]


# ---------------------------------------------------------------- X3 ----
import test_lxmf_outbound as TO
from test_lxmf_outbound import _with_env
from test_review_fixes2 import _int_clock
from harness import build_proof
import urns.packet as packet_mod


@_with_env()
def test_x3_late_proof_for_attempt_1_marks_delivered_and_stops_further_sends(e):
    restore = _int_clock(e)                      # whole-second wall clock, like MicroPython
    try:
        iface = e.ifaces[0]
        msg = e.send("hello")
        r1 = Transport.receipts[0]
        # allowance for the receiver's crypto time on top of the reference timeout
        assert r1.timeout >= Transport.first_hop_timeout(e.peer.hash) + 6 + const.RECEIPT_PEER_CRYPTO_GRACE
        e.advance_to(12)
        assert len(e.sent) == 2, "attempt 2 went out"
        e.advance_to(40)                         # attempt 1's receipt has timed out and been swept
        Transport._sweep_receipts()
        assert r1.status == packet_mod.PacketReceipt.FAILED
        assert r1 in Transport.receipts, "a timed-out receipt of a PENDING message stays matchable"
        n = len(e.sent)
        Transport.inbound(build_proof(r1.truncated_hash), iface)      # the slow peer's proof for attempt 1
        assert msg.state == TO.LXMessage.DELIVERED and len(e.ev("delivered")) == 1
        e.advance_to(120)
        assert len(e.sent) == n, "no further sends after the late proof"
        assert not e.router.pending_outbound
        assert not any(r.keep for r in Transport.receipts), "receipts released when the message concluded"
    finally:
        restore()


@_with_env()
def test_x3_kept_receipts_are_bounded_per_message_and_released_on_failure(e):
    restore = _int_clock(e)
    try:
        msg = e.send("hello")
        e.advance_to(200)                        # nobody proves: the whole retry sequence runs
        Transport._sweep_receipts()
        assert msg.state == TO.LXMessage.FAILED
        assert len(e.sent) >= 2
        assert not any(r.keep for r in Transport.receipts), "released on failure"
        assert len(Transport.receipts) <= const.MAX_RECEIPTS
        # while pending: at most KEEP_RECEIPTS stay pinned
        m2 = e.send("again")
        for t in (12, 24, 36, 48):
            e.advance_to(200 + t)
        pinned = [r for r in Transport.receipts if r.keep]
        assert len(pinned) <= TO.LXMessage.KEEP_RECEIPTS, len(pinned)
    finally:
        restore()


def test_x3_full_receipt_table_evicts_unkept_before_kept():
    Transport.receipts = []
    mk = lambda keep: types.SimpleNamespace(keep=keep)
    Transport.receipts.extend([mk(True), mk(False), mk(True)] + [mk(True)] * (const.MAX_RECEIPTS - 3))
    victim = Transport.receipts[1]
    pkt = types.SimpleNamespace(create_receipt=True)
    real = packet_mod.PacketReceipt
    packet_mod.PacketReceipt = lambda p: mk(False)
    try:
        Transport._create_receipt(pkt)
    finally:
        packet_mod.PacketReceipt = real
    assert all(r is not victim for r in Transport.receipts) and len(Transport.receipts) == const.MAX_RECEIPTS
    Transport.receipts = []


# ---------------------------------------------------------------- X4 ----
import test_announce_ingress as TI
from harness import build_announce_hdr1, build_announce_data


def _deferred_ingress():
    reset_transport()
    iface = MockInterface("tcp")
    Transport.interfaces = [iface]
    Transport.announce_inline = False
    return iface


def test_x4_forged_far_future_copy_does_not_displace_the_genuine_queued_announce():
    iface = _deferred_ingress()
    real = Identity.validate_announce
    seen = []

    def validate(packet_, only_validate_signature=False):
        e = Transport._announce_emitted(packet_)
        seen.append(e)
        return e < 10_000                      # the forged emission fails its signature
    Identity.validate_announce = staticmethod(validate)
    try:
        d = b"\xD4" * 16
        Transport.inbound(build_announce_hdr1(d, data=build_announce_data(emitted=1000), hops=1), iface)
        Transport.inbound(build_announce_hdr1(d, data=build_announce_data(emitted=99_999_999), hops=0), iface)
        assert len(Transport.announce_ingress) == 2, "both kept: the arrival cannot decide which one is genuine"
        Transport._service_announce_ingress()
        Transport._service_announce_ingress()
        assert sorted(seen) == [1000, 99_999_999], seen          # the genuine one WAS validated
        assert d in Transport.path_table, "the genuine announce installed the route"
    finally:
        Identity.validate_announce = real
        Transport.announce_inline = True


def test_x4_older_copy_with_fewer_hops_does_not_displace_a_newer_announce():
    iface = _deferred_ingress()
    d = b"\xD5" * 16
    try:
        Transport.inbound(build_announce_hdr1(d, data=build_announce_data(emitted=2000), hops=3), iface)   # newer, 4 hops
        Transport.inbound(build_announce_hdr1(d, data=build_announce_data(emitted=1000), hops=0), iface)   # old cached, 1 hop
        assert sorted(Transport._announce_emitted(p) for p in Transport.announce_ingress) == [1000, 2000]
        # identical copy of the newer one over a shorter path: still deduplicated, fewer hops kept
        Transport.inbound(build_announce_hdr1(d, data=build_announce_data(emitted=2000), hops=1), iface)
        q = [p for p in Transport.announce_ingress if Transport._announce_emitted(p) == 2000]
        assert len(q) == 1 and q[0].hops == 2
    finally:
        Transport.announce_inline = True


def test_x4_per_destination_and_global_bounds_hold_and_awaited_keeps_priority():
    iface = _deferred_ingress()
    try:
        d = b"\xD6" * 16
        for i in range(10):
            Transport.inbound(build_announce_hdr1(d, data=build_announce_data(emitted=100 + i)), iface)
        assert len([p for p in Transport.announce_ingress if p.destination_hash == d]) == const.MAX_ANNOUNCE_PER_DEST
        for i in range(3 * const.MAX_ANNOUNCE_INGRESS):
            Transport.inbound(build_announce_hdr1(bytes([0xE1, i & 0xFF]) + b"\x00" * 14,
                                                  data=build_announce_data(emitted=5)), iface)
        assert len(Transport.announce_ingress) <= const.MAX_ANNOUNCE_INGRESS
        aw = b"\xAB" * 16
        Transport._path_request_times[aw] = time.time()
        Transport.inbound(build_announce_hdr1(aw, data=build_announce_data(emitted=7)), iface)
        assert Transport.announce_ingress[0].destination_hash == aw
        # awaited, already two different ones queued: a third different one still gets in
        Transport.inbound(build_announce_hdr1(aw, data=build_announce_data(emitted=8)), iface)
        Transport.inbound(build_announce_hdr1(aw, data=build_announce_data(emitted=9)), iface)
        Transport.inbound(build_announce_hdr1(aw, data=build_announce_data(emitted=10)), iface)
        mine = [p for p in Transport.announce_ingress if p.destination_hash == aw]
        assert len(mine) == const.MAX_ANNOUNCE_PER_DEST and Transport._announce_emitted(Transport.announce_ingress[0]) == 10
    finally:
        Transport.announce_inline = True


# ---------------------------------------------------------------- X5 ----
@_with_env()
def test_x5_send_that_transmitted_nothing_is_not_sent_not_an_attempt_and_retries_when_an_interface_is_back(e):
    restore = _int_clock(e)
    try:
        iface = e.ifaces[0]
        iface.online = False                    # no interface can send: Transport.outbound() -> False
        per_msg = []
        msg = e.send("hello", sent_callback=lambda m: per_msg.append(m))
        assert msg.state == TO.LXMessage.OUTBOUND and not e.ev("sent") and not per_msg, "nothing left the node"
        assert msg.delivery_attempts == 0 and msg.sent_on_path in (False, None)
        for t in range(4, 41, 4):               # 10 ticks of nothing: no attempt burned, no rediscovery
            e.advance_to(t)
        assert msg.delivery_attempts == 0 and msg.state == TO.LXMessage.OUTBOUND
        assert e.req_times() == [], "no path rediscovery for a send that never happened"
        assert not e.ev("sent") and not per_msg
        assert not Transport.receipts, "no receipt for a packet that was not sent"
        iface.online = True                     # the interface comes back
        e.advance_to(44)
        assert msg.state == TO.LXMessage.SENT and msg.delivery_attempts == 1
        assert len(e.ev("sent")) == 1 and len(per_msg) == 1
    finally:
        restore()


@_with_env()
def test_x5_a_node_that_can_never_transmit_gives_up_after_the_bound(e):
    restore = _int_clock(e)
    try:
        e.ifaces[0].online = False
        msg = e.send("hello")
        e.advance_to(4 * (TO.LXMRouter.MAX_UNSENT_TICKS + 3))
        assert msg.state == TO.LXMessage.FAILED and len(e.ev("failed")) == 1
        assert msg.delivery_attempts == 0
    finally:
        restore()


# ---------------------------------------------------------------- X6 ----
import builtins
import announce_stamp_support as S6


def test_x6_emission_horizon_limits_flash_writes_and_stays_strictly_increasing():
    D = S6.RealDestination
    tmp = tempfile.mkdtemp()
    old_sp = S6.RealIdentity.storagepath
    S6.RealIdentity.storagepath = tmp
    writes = []
    real_open = builtins.open

    def counting_open(path, mode="r", *a, **k):
        if "w" in mode and "/emitted/" in str(path):
            writes.append(path)
        return real_open(path, mode, *a, **k)
    builtins.open = counting_open
    try:
        h = b"\x61" * 16
        D._emitted.clear()
        D._stored.clear()
        base = 1_795_285_656
        seen = []
        for i in range(100):                    # 100 announces over ~100 minutes with a real clock
            seen.append(D._emission_time(h, base + i * 60))
        assert seen == sorted(set(seen)) and len(writes) <= 11, len(writes)   # 100 min: one write per 10 min
        writes.clear()
        for i in range(100):                    # 100 announces within the same second (path responses)
            seen.append(D._emission_time(h, base + 6000))
        assert len(writes) <= 1, len(writes)
        assert seen == sorted(set(seen)), "strictly increasing"
        top = max(seen)
        # reboot with an unset clock: above everything emitted before
        D._emitted.clear()
        D._stored.clear()
        e = D._emission_time(h, 946684800 + 12)
        assert e > top
        # reboot with a correct clock that is past the horizon: adopted as is
        D._emitted.clear()
        D._stored.clear()
        far = top + 10 * D.PERSIST_EVERY
        assert D._emission_time(h, far) == far
    finally:
        builtins.open = real_open
        S6.RealIdentity.storagepath = old_sp
        D._emitted.clear()
        D._stored.clear()
        shutil.rmtree(tmp)


import shutil


# ---------------------------------------------------------------- X7 ----
stamps_mod = importlib.import_module("urns.stamps")
A7 = S6.announce_stamps


class _SlowCpu:
    """Deterministic MCU: every hash advances the tick clocks by `us` microseconds."""

    def __init__(self, us=1000):
        self.us = us
        self.t = 0
        self.real = (stamps_mod._ticks_us, const.ticks_ms, stamps_mod.sha256)
        real_sha = self.real[2]

        def sha(d):
            self.t += self.us
            return real_sha(d)
        stamps_mod._ticks_us = lambda: self.t
        const.ticks_ms = lambda: self.t // 1000
        stamps_mod.sha256 = sha

    def close(self):
        stamps_mod._ticks_us, const.ticks_ms, stamps_mod.sha256 = self.real


def test_x7_background_generation_uses_its_own_smaller_slice_target_interactive_keeps_80ms():
    stamps_mod.reset_native_probe()
    stamps_mod._native_state = -1
    stamps_mod._hc_state = -1                    # no native hashcash: the pure-Python path
    cpu = _SlowCpu(1000)                         # 1 ms per hash
    try:
        impossible = b"\x00" * 32               # no hit: only the slice sizes matter
        bg, inter = [], []
        for _ in range(14):
            bg.append(stamps_mod.find_hashcash(b"m" * 90, impossible, 14, background=True)[1])
            inter.append(stamps_mod.find_hashcash(b"m" * 90, impossible, 14)[1])
        assert 18 <= bg[-1] <= 22, bg                 # ~20 ms at 1 ms/hash
        assert 76 <= inter[-1] <= 84, inter           # ~80 ms: interactive is unchanged
        assert stamps_mod.hashcash_slice_size(background=True) != stamps_mod.hashcash_slice_size()
        # the two pacing objects do not feed each other
        stamps_mod.find_hashcash(b"m" * 90, impossible, 14, background=True, budget=3)
        n_inter = stamps_mod.hashcash_slice_size()
        stamps_mod.find_hashcash(b"m" * 90, impossible, 14, background=True)
        assert stamps_mod.hashcash_slice_size() == n_inter
    finally:
        cpu.close()
        stamps_mod.reset_native_probe()


def test_x7_announce_stamp_generation_task_runs_the_background_slices():
    stamps_mod.reset_native_probe()
    stamps_mod._hc_state = -1
    cpu = _SlowCpu(1000)
    d = S6.make_destination()
    tmp = tempfile.mkdtemp()
    A7.reset()
    A7.configure(enabled=True, value=60, allow_low=True, duty=50, storage_dir=tmp)   # unreachable
    tried = []
    real = stamps_mod.find_hashcash

    def spy(*a, **k):
        r = real(*a, **k)
        tried.append((r[1], k.get("background", False)))
        return r
    stamps_mod.find_hashcash = spy

    async def main():
        A7.request(d)
        for _ in range(400):
            await asyncio.sleep(0.002)
            if len(tried) >= 14:
                break
        for t in list(A7._tasks.values()):
            t.cancel()

    try:
        asyncio.run(main())
        assert tried and all(bg for _, bg in tried), "generation must ask for background slices"
        assert max(n for n, _ in tried[-3:]) <= 24, tried
    finally:
        stamps_mod.find_hashcash = real
        cpu.close()
        A7.reset()
        stamps_mod.reset_native_probe()
        shutil.rmtree(tmp)


# ---------------------------------------------------------------- X8 ----
x25519_mod = importlib.import_module("urns.crypto.x25519")


class _TickClock:
    """ticks_ms advances `step` ms on EVERY read: one read per ladder iteration models the
    ~5 ms an iteration takes on an ESP32 in pure Python."""

    def __init__(self, step):
        self.t = 0
        self.step = step
        self.real = (const.ticks_ms,)
        def now():
            self.t += self.step
            return self.t
        const.ticks_ms = now

    def close(self):
        const.ticks_ms, = self.real


def test_x8_u8_constant_and_resumable_ladder_match_the_x25519_implementation():
    P = stamps_mod._P
    lad = stamps_mod._Ladder(8, 9)
    assert lad.run() is True
    assert lad.x2 * pow(lad.z2, P - 2, P) % P == stamps_mod._U8
    import random
    for _ in range(3):
        k = random.getrandbits(255) & ~7 | (1 << 254)
        ref = x25519_mod._raw_curve25519(9, k)
        l = stamps_mod._Ladder(k, 9)
        n = 0
        while not l.run(budget_ms=1, t0=const.ticks_ms() - 1):    # one iteration per call
            n += 1
        assert n == 254 and l.x2 * pow(l.z2, P - 2, P) % P == ref


def test_x8_search_state_is_built_incrementally_one_slice_per_call():
    stamps_mod._native_state = -1
    stamps_mod.reset_search()
    clk = _TickClock(5)                                  # 5 ms per ladder iteration
    try:
        dest, sig = os.urandom(16), os.urandom(32)
        calls = 0
        spans = []
        prv = None
        while prv is None:
            t0 = clk.t
            prv, pub, tried = stamps_mod.find_link_key(dest, sig, 0, 32)     # cost 0: first candidate wins
            spans.append(clk.t - t0)
            calls += 1
            assert calls < 200
        # 2 ladders x 255 iterations x 5 ms = 2550 ms of start-up, in slices of ~80 ms
        assert calls >= 25, calls
        assert max(spans) <= stamps_mod.SLICE_TARGET_MS + 2 * clk.step, max(spans)   # budget + one iteration (+ clock reads)
        assert stamps_mod.stamp_valid(pub, 0, dest + sig)
    finally:
        clk.close()
        stamps_mod.reset_search()


def test_x8_ladder_and_batch_loop_follow_the_x25519_gc_discipline():
    real_collect = gc.collect
    count = [0]
    def counting():
        count[0] += 1
        return real_collect()
    old_mask = x25519_mod._gc_mask
    gc.collect = counting
    try:
        out = {}
        for mask in (1, 7):
            x25519_mod._gc_mask = mask
            count[0] = 0
            s = stamps_mod._PySearch()
            assert s.prepare() is True
            out[mask] = count[0]
        assert out[1] >= 2 * (255 // 2) - 2, out        # every 2nd iteration, both ladders
        assert out[7] <= 2 * (255 // 8) + 4 and out[7] >= 2 * (255 // 8) - 4, out
        # the candidate batch loop collects too (every (mask+1) batches)
        x25519_mod._gc_mask = 1
        count[0] = 0
        s.run(b"m" * 90, b"\x00" * 32, 32 * 8)
        assert count[0] >= 4, count[0]
    finally:
        gc.collect = real_collect
        x25519_mod._gc_mask = old_mask


import gc


# ---------------------------------------------------------------- X10 ----
class _CountingIdentity(TO.FakeIdentity):
    verifies = 0

    def validate(self, signature, message):
        _CountingIdentity.verifies += 1
        return True


def _three_receipts():
    reset_transport()
    iface = MockInterface("tcp")
    Transport.interfaces = [iface]
    recs = []
    for i in range(3):
        peer = _CountingIdentity(b"peer%d" % i)
        pkt = packet_mod.Packet(TO.FakeDest(peer), b"payload%d" % i)
        pkt.pack()
        recs.append(packet_mod.PacketReceipt(pkt))
    Transport.receipts = list(recs)
    _CountingIdentity.verifies = 0
    return iface, recs


def test_x10_implicit_proof_verifies_only_the_receipt_it_belongs_to():
    iface, recs = _three_receipts()
    Transport.inbound(build_proof(recs[1].truncated_hash), iface)
    assert _CountingIdentity.verifies == 1, _CountingIdentity.verifies
    assert [r.status for r in recs] == [1, packet_mod.PacketReceipt.DELIVERED, 1]
    # the LAST receipt in the list: still one verification (the loop used to try all before it)
    iface, recs = _three_receipts()
    Transport.inbound(build_proof(recs[2].truncated_hash), iface)
    assert _CountingIdentity.verifies == 1 and recs[2].status == packet_mod.PacketReceipt.DELIVERED
    # a proof for something we never sent: no verification at all
    iface, recs = _three_receipts()
    Transport.inbound(build_proof(b"\x99" * 16), iface)
    assert _CountingIdentity.verifies == 0 and all(r.status == 1 for r in recs)


def test_x10_explicit_proof_verifies_only_the_receipt_whose_hash_it_carries():
    iface, recs = _three_receipts()
    link_id = b"\x77" * 16
    Transport.inbound(build_proof(link_id, sig=recs[0].hash + b"\x01" * 64), iface)
    assert _CountingIdentity.verifies == 1 and recs[0].status == packet_mod.PacketReceipt.DELIVERED, _CountingIdentity.verifies
    iface, recs = _three_receipts()
    Transport.inbound(build_proof(link_id, sig=b"\xee" * 32 + b"\x01" * 64), iface)
    assert _CountingIdentity.verifies == 0 and all(r.status == 1 for r in recs)


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
