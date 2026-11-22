# LXMF outbound delivery scheduling (host-side, crypto-free). Run:
#   python3 firmware/tests/test_lxmf_outbound.py
#
# Mirrors reference LXMF 1.3.1 LXMRouter.process_outbound / schedule_attempt /
# request_path (the authority; every expected time below is derived by hand
# from its code and stated next to the assertion). A controllable clock
# replaces time.time(); the router is driven with process_outbound() at the
# 4 s job ticks (PROCESSING_INTERVAL), the way the router's job loop does.
#
# The scheduling NUMBERS (retry wait, attempt scaling, receipt timeout, medium
# path timeout, path-request wait) in REF below were produced by running the
# reference's own LXMRouter.schedule_attempt / destination_round_trip /
# path_request_wait and RNS Transport.first_hop_timeout / medium_path_timeout
# unmodified (rnsvenv, RNS 1.6.x + LXMF 1.3.1) against interfaces of the same
# bitrates; see the scratchpad refcalc.py used to generate them.

import sys
import os
import time
import random
import hashlib
import importlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
from harness import const, Transport, MockInterface, reset_transport, build_proof

lxmf = importlib.import_module("urns.lxmf")
link_mod = importlib.import_module("urns.link")
resource_mod = importlib.import_module("urns.resource")
packet_mod = importlib.import_module("urns.packet")
LXMessage = lxmf.LXMessage
LXMRouter = lxmf.LXMRouter

SF7 = 7 * (125000 / 2 ** 7) * (4.0 / 5)      # 5468.75 bit/s: urns lora.py formula, SF7/125 kHz/CR 4:5
TICK = 4                                      # LXMRouter.PROCESSING_INTERVAL

# (next_attempt_delay, max_attempts, receipt_timeout, round_trip, medium_path_timeout,
#  slow_interface_online, path_request_wait, slow_path)  -- from the reference code
REF = {
    ('fast_tcp_10Mbit', 'normal'):        (10.0, 5, None, 6.0008, 6.0008, False, 7, False),
    ('fast_tcp_10Mbit', 'path_request'):  (7.0, 5, None, 6.0008, 6.0008, False, 7, False),
    ('udp_1Mbit', 'normal'):              (10.0, 5, None, 6.008, 6.008, False, 7, False),
    ('udp_1Mbit', 'path_request'):        (7.0, 5, None, 6.008, 6.008, False, 7, False),
    ('sf7_125k', 'normal'):               (10.0, 5, 68.3886, 7.4629, 7.4629, False, 7.4629, False),
    ('sf7_125k', 'path_request'):         (7.463, 5, 68.3886, 7.4629, 7.4629, False, 7.4629, False),
    ('sf7_125k', 'normal_attempt1'):      (10.0, 5, 57.6571, 7.4629, 7.4629, False, 7.4629, False),
    ('sf7_125k', 'normal_attempt3'):      (10.0, 5, 36.1943, 7.4629, 7.4629, False, 7.4629, False),
    ('sf7_125k', 'no_path_normal'):       (10.0, 5, 68.3886, 7.4629, 7.4629, False, 7.4629, False),
    ('lora_300bps', 'normal'):            (32.667, 3, 188.0, 32.6667, 32.6667, True, 32.6667, True),
    ('lora_300bps', 'path_request'):      (32.667, 3, 188.0, 32.6667, 32.6667, True, 32.6667, True),
    ('lora_300bps', 'normal_attempt1'):   (32.667, 3, 142.0, 32.6667, 32.6667, True, 32.6667, True),
    ('lora_300bps', 'normal_attempt3'):   (32.667, 3, 50.0, 32.6667, 32.6667, True, 32.6667, True),
    ('lora_300bps', 'no_path_normal'):    (32.667, 3, 188.0, 32.6667, 32.6667, True, 32.6667, True),
    ('lora_300_next_tcp', 'normal'):      (10.0, 5, None, 6.0008, 32.6667, True, 32.6667, False),
    ('lora_300_next_tcp', 'path_request'): (32.667, 3, 188.0, 6.0008, 32.6667, True, 32.6667, False),
    ('lora_300_next_tcp', 'no_path_normal'): (32.667, 3, 188.0, 32.6667, 32.6667, True, 32.6667, True),
}
CASES = {   # name -> (online interface bitrates, bitrate of the next-hop interface)
    'fast_tcp_10Mbit': ([10_000_000], 10_000_000),
    'udp_1Mbit': ([1_000_000], 1_000_000),
    'sf7_125k': ([SF7], SF7),
    'lora_300bps': ([300], 300),
    'lora_300_next_tcp': ([300, 10_000_000], 10_000_000),
}
# reference RNS Transport.first_hop_timeout for a 1-hop path on those interfaces
REF_FIRST_HOP = {'fast_tcp_10Mbit': 6.0004, 'udp_1Mbit': 6.004, 'sf7_125k': 6.7314,
                 'lora_300bps': 19.3333, 'lora_300_next_tcp': 6.0004}


# ------------------------------------------------------------------ fakes ----

class FakeIdentity:
    def __init__(self, seed):
        self.hash = hashlib.sha256(seed).digest()[:16]
        self.hexhash = self.hash.hex()
        self._seed = seed

    def sign(self, data):
        return hashlib.sha512(self._seed + data).digest()

    def get_public_key(self):
        return hashlib.sha512(self._seed).digest()

    def validate(self, signature, message):
        return True


class FakeDest:
    """Minimal OUT SINGLE destination: real Packets can be built and sent on it."""
    OUT = 0x12
    SINGLE = 0x00

    def __init__(self, identity, *args):
        self.identity = identity
        self.hash = identity.hash
        self.hexhash = identity.hash.hex()
        self.type = const.DEST_SINGLE

    def sign(self, data):
        return self.identity.sign(data)

    def encrypt(self, plaintext):
        return os.urandom(8) + plaintext      # fresh bytes per attempt, like the real IV


class FakeReceipt:
    status = 0x01
    timeout = 6.0

    def __init__(self):
        self.delivery_callback = None
        self.timeout_callback = None

    def set_delivery_callback(self, cb):
        self.delivery_callback = cb

    def set_timeout_callback(self, cb):
        self.timeout_callback = cb

    def set_timeout(self, t):
        self.timeout = float(t)


class FakeLink:
    """OutgoingLink stand-in. status: 0 PENDING, 1 ACTIVE, 2 CLOSED."""
    instances = []

    def __init__(self, destination, established_callback=None, closed_callback=None, **kw):
        self.destination = destination
        self.status = 0x00
        self.activated_at = None
        self.close_reason = None
        self.link_id = bytes([len(FakeLink.instances) + 1]) * 16
        self.calls = []
        self.receipts = []
        self.resource_concluded_callback = None
        self.last_activity = time.time()
        self.last_outbound = time.time()
        self._established = established_callback
        self._closed = closed_callback
        FakeLink.instances.append(self)

    def establish(self):
        self.status = 0x01
        self.activated_at = time.time()
        self._established(self)

    def close(self, reason=None):
        self.status = 0x02
        self.close_reason = reason
        if self._closed:
            self._closed(self)

    def identify(self, identity):
        self.calls.append(("identify", identity))

    def send(self, data, context=0, create_receipt=False):
        self.calls.append(("send", bytes(data)))
        if create_receipt:
            r = FakeReceipt()
            self.receipts.append(r)
            return r
        return None

    def teardown(self):
        self.calls.append(("teardown", None))
        self.status = 0x02


class FakeResource:
    instances = []

    def __init__(self, link, data, is_response=False, request_id=None):
        self.link = link
        self.data = data
        self.status = 0x01
        self.is_initiator = True
        self.cancelled = False
        FakeResource.instances.append(self)

    def get_progress(self):
        return 0.5

    def cancel(self, signal=True):
        self.cancelled = True
        self.status = resource_mod.FAILED
        self.link.resource_concluded_callback(self)


class Env:
    """Clock + interfaces + router wired to the fakes; records path requests."""

    def __init__(self, bitrates=(10_000_000,), hop_bitrate=None, with_path=True,
                 peer=b"peer", method=None, identity_known=True):
        reset_transport()
        Transport.stamp_path_requests = False
        self.real_time = time.time
        self.t = [1_000_000.0]
        time.time = lambda: self.t[0]
        self.t0 = self.t[0]
        self.ifaces = [MockInterface("if%d" % i, bitrate=b) for i, b in enumerate(bitrates)]
        Transport.interfaces = list(self.ifaces)
        self.hop_if = self.ifaces[-1] if hop_bitrate is None else self.ifaces[bitrates.index(hop_bitrate)]
        self.requests = []                 # (t, dest) of every Transport.request_path
        self.real_rp = Transport.request_path
        Transport.request_path = staticmethod(lambda h, *a, **k: self.requests.append((self.now(), h)))
        self.old = (link_mod.OutgoingLink, resource_mod.Resource, lxmf.Destination)
        link_mod.OutgoingLink = FakeLink
        resource_mod.Resource = FakeResource
        lxmf.Destination = FakeDest
        FakeLink.instances = []
        FakeResource.instances = []
        self.me = FakeIdentity(b"me")
        self.peer = FakeIdentity(peer)
        self.router = LXMRouter()
        self.router._slow_crypto = False       # the reference timing rules are what these tests pin down
        self.router.delivery_identity = self.me
        self.router.delivery_destination = FakeDest(self.me)
        if identity_known:
            lxmf.Identity.known[self.peer.hash] = self.peer
        if with_path:
            self.set_path(True)
        self.sent = []                     # (t, plaintext) of every opportunistic packet
        self.events = []                   # (t, name, msg)
        self.router.register_outbound_callbacks(
            sent=lambda m: self.events.append((self.now(), "sent", m)),
            delivered=lambda m: self.events.append((self.now(), "delivered", m)),
            failed=lambda m: self.events.append((self.now(), "failed", m)))
        self._orig_encrypt = FakeDest.encrypt
        FakeDest.encrypt = lambda d, p: (self.sent.append((self.now(), bytes(p))), os.urandom(8) + p)[1]

    def close(self):
        FakeDest.encrypt = self._orig_encrypt
        time.time = self.real_time
        Transport.request_path = self.real_rp
        link_mod.OutgoingLink, resource_mod.Resource, lxmf.Destination = self.old
        lxmf.Identity.known.clear()
        Transport.reachable_destinations.clear()
        Transport.path_table.clear()
        Transport.interfaces = []
        Transport.receipts = []
        Transport.stamp_path_requests = True

    def now(self):
        return round(self.t[0] - self.t0, 3)

    def set_path(self, present, hops=1):
        h = self.peer.hash
        if present:
            Transport.reachable_destinations[h] = self.t[0]
            Transport.path_table[h] = [self.t[0], h, hops, self.t[0] + 1e9, self.hop_if, b"\x00" * 16, 0]
        else:
            Transport.reachable_destinations.pop(h, None)
            Transport.path_table.pop(h, None)

    def advance_to(self, when):
        """Run the 4 s job ticks until `when` seconds after t0 (inclusive)."""
        while True:
            nxt = (int((self.t[0] - self.t0) // TICK) + 1) * TICK
            if nxt > when:
                break
            self.t[0] = self.t0 + nxt
            self.router.process_outbound()
        self.t[0] = self.t0 + when

    def send(self, text="hello", **kw):
        return self.router.send_message(self.peer.hash, text, **kw)

    def req_times(self):
        return [t for t, h in self.requests if h == self.peer.hash]

    def sent_times(self):
        return [t for t, _ in self.sent]

    def ev(self, name):
        return [(t, m) for t, n, m in self.events if n == name]


def _with_env(**kw):
    def deco(fn):
        def run():
            e = Env(**kw)
            try:
                fn(e)
            finally:
                e.close()
        run.__name__ = fn.__name__
        return run
    return deco


# ------------------------------------------------- scheduling numbers ----

def _sched(name, kind):
    bitrates, hop = CASES[name]
    e = Env(bitrates=tuple(bitrates), hop_bitrate=hop, with_path=(kind != 'no_path_normal'))
    try:
        r = e.router
        m = LXMessage(destination=None, source=None, content=b"x", destination_hash=e.peer.hash)
        attempts = {'normal_attempt1': 1, 'normal_attempt3': 3}.get(kind, 0)
        m.delivery_attempts = attempts
        random.random, saved = (lambda: 0.0), random.random      # jitter lower bound
        try:
            r.schedule_attempt(m, e.peer.hash, path_request=(kind == 'path_request'))
        finally:
            random.random = saved
        return (round(m.next_delivery_attempt - e.t0, 3), m.max_delivery_attempts,
                None if m.receipt_timeout is None else round(m.receipt_timeout, 4),
                round(r.destination_round_trip(e.peer.hash), 4), round(r.medium_path_timeout(), 4),
                r.slow_interface_online(), round(r.path_request_wait(), 4),
                r.slow_path(e.peer.hash))
    finally:
        e.close()


def test_schedule_numbers_match_the_reference():
    for (name, kind), expected in sorted(REF.items()):
        got = _sched(name, kind)
        assert got == tuple(expected), (name, kind, got, expected)


def test_first_hop_timeout_and_receipt_timeout_match_the_reference():
    for name, (bitrates, hop) in CASES.items():
        e = Env(bitrates=tuple(bitrates), hop_bitrate=hop)
        try:
            assert round(Transport.first_hop_timeout(e.peer.hash), 4) == REF_FIRST_HOP[name], name
            # reference PacketReceipt: first_hop_timeout + 6 s per hop
            pkt = packet_mod.Packet(FakeDest(e.peer), b"x")
            pkt.pack()
            rc = packet_mod.PacketReceipt(pkt)
            # reference timeout + the port's allowance for the receiver's crypto time
            assert abs(rc.timeout - (REF_FIRST_HOP[name] + 6 * 1 + const.RECEIPT_PEER_CRYPTO_GRACE)) < 1e-3, (name, rc.timeout)
        finally:
            e.close()
    e = Env(with_path=False)
    try:
        assert Transport.first_hop_timeout(e.peer.hash) == 6          # unknown next hop: grace only
    finally:
        e.close()


def test_lowest_bitrate_counts_only_online_interfaces_with_a_bitrate():
    e = Env(bitrates=(300, 10_000_000))
    try:
        assert Transport.lowest_interface_bitrate() == 300
        e.ifaces[0].online = False
        assert Transport.lowest_interface_bitrate() == 10_000_000
        assert Transport.medium_path_timeout() < 7 and not Transport.slow_interface_online()
        e.ifaces[1].bitrate = 0
        assert Transport.lowest_interface_bitrate() is None
        assert Transport.medium_path_timeout() == 0          # unknown
        e.ifaces[0].online = True
        assert Transport.medium_path_timeout() == 2 * (500 * 8 / 300) + 6      # 32.667
    finally:
        e.close()


def test_jitter_is_bounded_by_the_mtu_time_on_a_300_bit_link():
    e = Env(bitrates=(300,))
    try:
        mtu_time = (REF[('lora_300bps', 'normal')][3] - 6) / 2          # 13.333
        lo = hi = None
        for rv in (0.0, 0.999999):
            m = LXMessage(destination=None, source=None, content=b"x", destination_hash=e.peer.hash)
            random.random, saved = (lambda v=rv: v), random.random
            try:
                e.router.schedule_attempt(m, e.peer.hash)
            finally:
                random.random = saved
            d = m.next_delivery_attempt - e.t0
            lo, hi = (d, hi) if rv == 0.0 else (lo, d)
        assert abs(lo - 32.667) < 1e-3 and abs(hi - (32.667 + mtu_time)) < 1e-3
        # a fast link has no jitter and no receipt timeout (mtu_time <= 0.5 s)
        e2 = Env(bitrates=(10_000_000,))
        try:
            m = LXMessage(destination=None, source=None, content=b"x", destination_hash=e2.peer.hash)
            e2.router.schedule_attempt(m, e2.peer.hash)
            assert m.next_delivery_attempt == e2.t0 + 10 and m.receipt_timeout is None
        finally:
            e2.close()
    finally:
        e.close()


# ------------------------------------------- opportunistic: delivery ----

@_with_env()
def test_proof_marks_delivered_and_callbacks_fire_once(e):
    seen = []
    msg = e.send("hello", delivery_callback=lambda m: seen.append(("msg", m.state)),
                 sent_callback=lambda m: seen.append(("sent", m.state)))
    assert msg is not None and msg.method == LXMessage.OPPORTUNISTIC
    assert msg.state == LXMessage.SENT and msg.delivery_attempts == 1
    assert e.sent_times() == [0.0]
    assert [n for n, _ in seen] == ["sent"] and len(e.ev("delivered")) == 0       # not delivered at send time
    assert len(Transport.receipts) == 1
    receipt = Transport.receipts[0]
    # the proof comes back as an inbound PROOF packet addressed to the packet's truncated hash
    e.t[0] += 1
    Transport.inbound(build_proof(receipt.truncated_hash, b"\x00" * 64), e.ifaces[0])
    assert msg.state == LXMessage.DELIVERED and msg.progress == 1.0
    assert [n for n, _ in seen] == ["sent", "msg"] and seen[1][1] == LXMessage.DELIVERED
    assert len(e.ev("delivered")) == 1 and len(e.ev("sent")) == 1
    e.router.process_outbound()
    assert msg not in e.router.pending_outbound
    # a second proof for the same message changes nothing
    msg._mark_delivered()
    assert len(e.ev("delivered")) == 1
    e.advance_to(60)
    assert e.sent_times() == [0.0] and not e.ev("failed")                    # never resent


@_with_env()
def test_proof_that_was_already_delivered_at_send_time_counts(e):
    real = packet_mod.PacketReceipt.__init__

    def init(self, packet):
        real(self, packet)
        self.status = packet_mod.PacketReceipt.DELIVERED
    packet_mod.PacketReceipt.__init__ = init
    try:
        msg = e.send("hello")
    finally:
        packet_mod.PacketReceipt.__init__ = real
    assert msg.state == LXMessage.DELIVERED and len(e.ev("delivered")) == 1


@_with_env()
def test_no_proof_resends_after_retry_wait_with_identical_packed_bytes(e):
    msg = e.send("hello")
    packed0 = msg.packed
    stamp0 = msg.stamp
    # known path, fast link: retry wait = max(10, round_trip 6.0008) = 10 s; ticks every 4 s,
    # `time > next_delivery_attempt` is strict -> first tick after t=10 is t=12
    e.advance_to(11)
    assert e.sent_times() == [0.0]
    e.advance_to(12)
    assert e.sent_times() == [0.0, 12.0]
    assert msg.packed is packed0 and msg.stamp == stamp0            # same bytes, never repacked or re-stamped
    plain = [p for _, p in e.sent]
    assert plain[0] == plain[1] == packed0[16:]                     # the SAME LXMF payload each time


# ------------------------------------------- opportunistic: sequences ----

@_with_env()
def test_known_path_sequence_send_send_rediscover_then_fail(e):
    # Derived from LXMRouter.process_outbound with a fast link (retry wait 10 s,
    # path_request_wait 7 s), path known, nobody ever proves:
    #   t=0   attempt 1: send                      (next attempt due after 10)
    #   t=12  attempt 2: send                      (first tick > 10)
    #   t=24  attempts == MAX_PATHLESS_TRIES+1 and sent_on_path -> REDISCOVERY: attempt 3,
    #         path dropped, request 0.5 s later, next = t + path_request_wait = 31
    #   t=32  no path, attempts>=1 -> counted path request (attempt 4), next = 39
    #   t=40  path request (attempt 5), next 47;  t=48 path request (attempt 6), next 55
    #   t=56  attempts 6 > limit 5 and due -> fail_message
    msg = e.send()
    e.advance_to(100)
    assert e.sent_times() == [0.0, 12.0]
    assert e.req_times() == [24.0, 32.0, 40.0, 48.0]                # rediscovery (+0.5 s: no loop -> at once), then counted requests
    assert len(e.ev("failed")) == 1 and e.ev("failed")[0][0] == 56.0
    assert msg.state == LXMessage.FAILED and msg.delivery_attempts == 6 and msg not in e.router.pending_outbound
    assert not Transport.has_path(e.peer.hash)                      # the dropped path never came back


@_with_env()
def test_known_path_sequence_when_the_path_comes_back_after_rediscovery(e):
    #   t=0 send, t=12 send, t=24 rediscovery (attempt 3, next 31); the path returns at t=26:
    #   tick t=28: awaiting_path and has_path -> due EARLY -> attempt 4: send (next 38)
    #   t=40 attempt 5: send (next 50), t=52 attempt 6: send (next 62), t=64: attempts 6 > 5 -> fail
    msg = e.send()
    e.advance_to(25)
    assert e.sent_times() == [0.0, 12.0] and msg.awaiting_path and msg.delivery_attempts == 3
    e.t[0] = e.t0 + 26
    e.set_path(True)
    e.advance_to(100)
    assert e.sent_times() == [0.0, 12.0, 28.0, 40.0, 52.0]
    assert e.ev("failed")[0][0] == 64.0 and msg.delivery_attempts == 6


@_with_env(with_path=False)
def test_no_path_sequence_preemptive_request_pathless_send_counted_requests(e):
    # Derived from handle_outbound + process_outbound, no path ever, fast link:
    #   t=0   pre-emptive path request, schedule(path_request=True): next = 7, awaiting_path;
    #         NOT counted as an attempt, not processed at submit
    #   t=8   (first tick > 7) attempts 0 < MAX_PATHLESS_TRIES: ONE pathless send (attempt 1), next = 18
    #   t=20  attempts 1 >= 1 and no path: counted path request (attempt 2), next = 27
    #   t=28 (3), t=36 (4), t=44 (5), t=52 (6): each a request, next +7;  t=60: 6 > 5 and due -> fail
    msg = e.send()
    assert msg.state == LXMessage.OUTBOUND and msg.delivery_attempts == 0 and msg.awaiting_path
    assert e.req_times() == [0.0] and e.sent_times() == []
    e.advance_to(100)
    assert e.sent_times() == [8.0]                                   # one pathless send, as the reference
    assert e.req_times() == [0.0, 20.0, 28.0, 36.0, 44.0, 52.0]
    assert e.ev("failed")[0][0] == 60.0 and msg.delivery_attempts == 6
    assert msg.state == LXMessage.FAILED


@_with_env(with_path=False)
def test_early_send_when_the_path_appears(e):
    msg = e.send()
    e.advance_to(2)
    e.set_path(True)                         # path response arrives at t=2; next attempt was due at 7
    e.advance_to(4)                          # tick t=4: awaiting_path and has_path -> due now
    assert e.sent_times() == [4.0] and msg.sent_on_path and msg.delivery_attempts == 1
    assert msg.state == LXMessage.SENT


@_with_env(bitrates=(SF7,))
def test_sf7_receipt_timeout_is_extended_like_the_reference(e):
    # SF7/125 kHz: round trip 7.4629 -> mtu_time 0.7314 > 0.5: receipt_timeout =
    # (max(10,7.4629)+0.7314) * max(1, 5-1+1) + 4 = 57.6571 (reference value for attempt 1)
    msg = e.send()
    assert abs(msg.receipt_timeout - 57.6571) < 1e-3
    receipt = Transport.receipts[0]
    assert abs(receipt.timeout - 57.6571) < 1e-3          # max(first_hop 6.7314 + 6, receipt_timeout)
    # not a slow path: SF7 is above the 2 kbit/s line
    assert not e.router.slow_interface_online() and not msg.sent_on_slow_path


# ------------------------------------------------ path request debounce ----

@_with_env()
def test_path_request_is_not_repeated_inside_the_window(e):
    r, h = e.router, e.peer.hash
    assert r.request_path(h) is True
    e.t[0] += 6.9
    assert r.request_path(h) is False and r.path_request_pending(h)
    e.t[0] += 0.2                                          # 7.1 s > path_request_wait (7)
    assert r.request_path(h) is True and r.path_request_pending(h)
    assert len(e.requests) == 2
    other = b"\x77" * 16
    assert r.request_path(other) is True                    # per destination
    assert len(r.path_request_times) <= LXMRouter._PATH_REQ_TIMES_MAX


@_with_env(bitrates=(300,))
def test_path_request_debounce_is_60_s_on_a_slow_interface(e):
    r, h = e.router, e.peer.hash
    assert r.slow_interface_online()
    assert r.path_request_wait() > 32.6
    assert r.request_path(h) is True
    e.t[0] += 59
    assert r.request_path(h) is False
    e.t[0] += 1.1
    assert r.request_path(h) is True
    assert [t for t, _ in e.requests] == [0.0, 60.1]


@_with_env(bitrates=(300, 10_000_000), hop_bitrate=10_000_000)
def test_slow_interface_online_window_applies_even_when_the_path_is_fast(e):
    # a slow interface anywhere makes requests expensive for everyone (reference slow_interface_online)
    assert e.router.slow_interface_online()
    e.router.request_path(e.peer.hash)
    e.t[0] += 40                       # > path_request_wait (32.7) but < 60
    assert e.router.request_path(e.peer.hash) is False


@_with_env(bitrates=(300,))
def test_ensure_path_rerequest_interval_is_medium_aware(e):
    assert Transport.path_rerequest_interval() == 60                 # slow interface: never under 60 s
    e.ifaces[0].bitrate = 5468.75
    assert abs(Transport.path_rerequest_interval() - 7.4629) < 1e-3  # >= path_request_wait()
    e.ifaces[0].bitrate = 10_000_000
    assert Transport.path_rerequest_interval() == 7                  # was 5 s


# --------------------------------------------------------- slow path ----

@_with_env(bitrates=(300,))
def test_slow_path_hold_same_target_and_any_slow_message_in_flight(e):
    other = FakeIdentity(b"other")
    lxmf.Identity.known[other.hash] = other
    Transport.reachable_destinations[other.hash] = e.t[0]
    Transport.path_table[other.hash] = [e.t[0], other.hash, 1, e.t[0] + 1e9, e.hop_if, b"\x00" * 16, 0]
    a = e.send("a")
    assert a.state == LXMessage.SENT and a.sent_on_slow_path        # 300 bit/s: round trip 32.7 s > 10 s
    b = e.send("b")                                                 # same target: held behind a
    assert b.state == LXMessage.OUTBOUND and b.delivery_attempts == 0
    c = e.router.send_message(other.hash, "c")                      # other target, but a slow message is in flight
    assert c.state == LXMessage.OUTBOUND and c.delivery_attempts == 0
    e.advance_to(28)                                                # still held at every tick
    assert b.delivery_attempts == 0 and c.delivery_attempts == 0
    # a's proof arrives: it was sent on the slow path -> processing runs AT ONCE, not at the next tick
    a._mark_delivered()
    assert a not in e.router.pending_outbound
    assert b.state == LXMessage.SENT and b.delivery_attempts == 1
    assert c.state == LXMessage.OUTBOUND                           # b (slow, in flight) now holds c
    b._mark_delivered()
    assert c.state == LXMessage.SENT


@_with_env()
def test_fast_path_is_never_held(e):
    a = e.send("a")
    b = e.send("b")
    assert a.state == b.state == LXMessage.SENT
    a._mark_delivered()                                             # not a slow path: no immediate pass needed
    assert e.router.slow_path_held(b, e.peer.hash) is False


# -------------------------------------------------------------- DIRECT ----

def _big():
    return "x" * 600


@_with_env()
def test_direct_opens_a_link_identifies_then_sends_packet_and_delivers(e):
    msg = e.send("short", desired_method=LXMessage.DIRECT)
    assert msg.state == LXMessage.OUTBOUND and msg.delivery_attempts == 1 and len(FakeLink.instances) == 1
    link = FakeLink.instances[0]
    assert msg.progress == 0.03
    link.establish()                          # established callback -> identify, then process -> send
    kinds = [c[0] for c in link.calls]
    assert kinds == ["identify", "send"], kinds
    assert msg.state == LXMessage.SENDING and len(e.ev("sent")) == 1
    link.receipts[0].delivery_callback(link.receipts[0])
    assert msg.state == LXMessage.DELIVERED and len(e.ev("delivered")) == 1
    # PORT: the link is closed as soon as nothing queued needs it (the reference holds it 10 min)
    assert link.status == 0x02 and e.peer.hash not in e.router.direct_links
    assert ("teardown", None) in link.calls


@_with_env()
def test_direct_messages_to_one_peer_share_a_link_closed_after_the_last(e):
    m1 = e.send("one", desired_method=LXMessage.DIRECT)
    m2 = e.send("two", desired_method=LXMessage.DIRECT)
    assert len(FakeLink.instances) == 1
    link = FakeLink.instances[0]
    link.establish()
    assert [c[0] for c in link.calls] == ["identify", "send", "send"]
    link.receipts[0].delivery_callback(None)
    assert m1.state == LXMessage.DELIVERED and link.status == 0x01   # m2 still needs it
    link.receipts[1].delivery_callback(None)
    assert m2.state == LXMessage.DELIVERED and link.status == 0x02


@_with_env()
def test_direct_pending_link_waits_and_closed_link_reschedules(e):
    # attempt 1 at t=0 opens link 1 (next attempt scheduled for t=10)
    msg = e.send("x", desired_method=LXMessage.DIRECT)
    link = FakeLink.instances[0]
    e.advance_to(8)
    assert len(FakeLink.instances) == 1 and msg.delivery_attempts == 1        # pending: wait
    link.close()                                                  # never activated (establishment timeout)
    e.advance_to(12)                                              # tick t=12: CLOSED branch
    #  -> path_request_retried: debounced request, link dropped, attempts+1 < 5 -> schedule next (now+10)
    assert e.req_times() == [12.0] and msg.path_request_retried
    assert link not in e.router.direct_links.values() and len(FakeLink.instances) == 1
    e.advance_to(24)                                              # t=24 > 22: no link -> attempt 2, link 2
    assert len(FakeLink.instances) == 2 and msg.delivery_attempts == 2
    FakeLink.instances[1].close()
    e.advance_to(28)                                              # CLOSED again: already retried, no new request
    assert e.req_times() == [12.0]


@_with_env()
def test_direct_closed_after_activation_asks_for_a_path(e):
    msg = e.send("x", desired_method=LXMessage.DIRECT)
    link = FakeLink.instances[0]
    link.establish()
    assert msg.state == LXMessage.SENDING
    link.close()                                                   # peer went away with the proof outstanding
    e.advance_to(4)
    assert e.req_times() == [4.0]                                  # activated link closed: always requests


@_with_env()
def test_direct_packet_receipt_timeout_tears_down_and_returns_to_outbound(e):
    msg = e.send("x", desired_method=LXMessage.DIRECT)
    link = FakeLink.instances[0]
    link.establish()
    receipt = link.receipts[0]
    receipt.timeout_callback(receipt)
    assert ("teardown", None) in link.calls and link.status == 0x02
    assert msg.state == LXMessage.OUTBOUND
    # next pass: link closed -> rescheduled; after the retry wait a new link carries it
    e.advance_to(4)
    assert len(FakeLink.instances) == 1
    e.advance_to(16)
    assert len(FakeLink.instances) == 2 and msg.delivery_attempts == 2


@_with_env()
def test_direct_resource_transfer_for_large_messages_and_failure_retries(e):
    msg = e.send(_big(), desired_method=LXMessage.DIRECT)
    assert msg.representation == LXMessage.RESOURCE
    link = FakeLink.instances[0]
    link.establish()
    res = FakeResource.instances[0]
    assert msg.state == LXMessage.SENDING and res.lxm is msg and res.data == msg.packed
    e.advance_to(4)
    assert abs(msg.progress - 0.55) < 1e-9                         # 0.10 + 0.5 * 0.90
    res.status = resource_mod.FAILED
    link.resource_concluded_callback(res)                          # failed transfer: teardown, OUTBOUND
    assert msg.state == LXMessage.OUTBOUND and link.status == 0x02
    e.advance_to(16)           # tick t=8 sees the CLOSED link: drops it, next attempt = 8 + 10 = 18
    assert len(FakeLink.instances) == 1
    e.advance_to(20)
    assert len(FakeLink.instances) == 2 and msg.delivery_attempts == 2
    FakeLink.instances[1].establish()
    res2 = FakeResource.instances[1]
    assert res2.data == msg.packed                                 # same packed bytes
    res2.status = resource_mod.COMPLETE
    FakeLink.instances[1].resource_concluded_callback(res2)
    assert msg.state == LXMessage.DELIVERED and len(e.ev("delivered")) == 1


@_with_env()
def test_direct_rejected_resource_is_final(e):
    msg = e.send(_big(), desired_method=LXMessage.DIRECT)
    link = FakeLink.instances[0]
    link.establish()
    res = FakeResource.instances[0]
    res.status = resource_mod.REJECTED
    link.resource_concluded_callback(res)
    assert msg.state == LXMessage.REJECTED
    e.router.process_outbound()                       # next pass reports it (reference: no retry)
    assert msg.state == LXMessage.REJECTED and len(e.ev("failed")) == 1
    assert msg not in e.router.pending_outbound
    e.advance_to(120)
    assert len(FakeLink.instances) == 1 and len(e.ev("failed")) == 1


@_with_env(with_path=False)
def test_direct_without_a_path_requests_one_and_fails_after_the_attempts(e):
    # DIRECT, no path, fast link: due at once: attempt 1, schedule(retry), no path -> request +
    # schedule(path_request=True) (next 7); each later due pass is attempt+1 with a request,
    # until attempts reach the limit (5) -> fail_message at the pass where attempts == 5
    msg = e.send("x", desired_method=LXMessage.DIRECT)
    assert msg.delivery_attempts == 1 and e.req_times() == [0.0]
    e.advance_to(200)
    assert len(FakeLink.instances) == 0
    assert e.req_times() == [0.0, 8.0, 16.0, 24.0]                 # attempts 1..4 request; attempt 5 == limit -> fail
    assert e.ev("failed")[0][0] == 32.0 and msg.state == LXMessage.FAILED and msg.delivery_attempts == 5


@_with_env()
def test_direct_link_stamp_failure_fails_fast_without_retries(e):
    msg = e.send("x", desired_method=LXMessage.DIRECT)
    FakeLink.instances[0].close(reason="stamp")                    # closed callback: processed at once
    assert msg.state == LXMessage.FAILED and len(e.ev("failed")) == 1
    e.advance_to(200)
    assert len(FakeLink.instances) == 1


@_with_env()
def test_failed_direct_message_also_releases_its_link(e):
    msg = e.send("x", desired_method=LXMessage.DIRECT)
    link = FakeLink.instances[0]
    link.establish()
    e.router.fail_message(msg)
    assert link.status == 0x02 and e.peer.hash not in e.router.direct_links


# --------------------------------------------------- queue, cancel, etc ----

@_with_env(with_path=False)
def test_queue_bound_rejects_cleanly_when_full(e):
    bound = LXMRouter.MAX_PENDING_OUTBOUND
    assert bound == (8 if const._BIG_RAM else 4)
    msgs = [e.send("m%d" % i) for i in range(bound)]
    assert all(m is not None for m in msgs) and len(e.router.pending_outbound) == bound
    assert e.send("one too many") is None                          # rejected, visibly
    assert len(e.router.pending_outbound) == bound and not e.ev("failed")
    # a concluded message frees a slot
    e.router.fail_message(msgs[0])
    assert e.send("fits again") is not None


@_with_env()
def test_cancel_outbound_removes_and_reports(e):
    msg = e.send("x", desired_method=LXMessage.DIRECT)
    assert e.router.cancel_outbound(msg.message_id) is True
    assert msg.state == LXMessage.CANCELLED and msg not in e.router.pending_outbound
    assert len(e.ev("failed")) == 1
    assert e.router.cancel_outbound(b"\x00" * 32) is False


@_with_env()
def test_cancel_during_resource_transfer_does_not_retry(e):
    msg = e.send(_big(), desired_method=LXMessage.DIRECT)
    link = FakeLink.instances[0]
    link.establish()
    res = FakeResource.instances[0]
    assert e.router.cancel_outbound(msg.message_id)
    assert res.cancelled and msg.state == LXMessage.CANCELLED          # not flipped back to OUTBOUND
    e.advance_to(60)
    assert len(FakeLink.instances) == 1


@_with_env()
def test_stamp_is_computed_once_and_reused_across_retries(e):
    ticket = b"\x42" * 16
    e.router.outbound_tickets[e.peer.hash] = [time.time() + 1e6 + 1_700_000_000, ticket]
    calls = []
    real_pack = LXMessage.pack
    LXMessage.pack = lambda self, *a, **k: (calls.append(1), real_pack(self, *a, **k))[1]
    try:
        msg = e.send("stamped")
        e.advance_to(30)
    finally:
        LXMessage.pack = real_pack
    assert len(calls) == 1                                           # packed (and signed, stamped) once
    assert msg.stamp == hashlib.sha256(ticket + msg.message_id).digest()[:16]
    payloads = [p for _, p in e.sent]
    assert len(payloads) >= 2 and all(p == msg.packed[16:] for p in payloads)


@_with_env()
def test_pow_stamp_is_computed_once_across_retries(e):
    cost_calls = []
    real = LXMessage._pow_stamp
    LXMessage._pow_stamp = lambda self, cost: (cost_calls.append(cost), b"\x01" * 32)[1]
    e.router.peers[e.peer.hash] = {"stamp_cost": 4}              # inline-sized cost (<= 6): searched in pack()
    try:
        msg = e.send("work")
        e.advance_to(40)
    finally:
        LXMessage._pow_stamp = real
    assert cost_calls == [4]
    assert all(p == msg.packed[16:] for _, p in e.sent) and len(e.sent) == 2


@_with_env(identity_known=False, with_path=False)
def test_unknown_identity_requests_a_path_and_sends_once_it_is_learned(e):
    # PORT-specific: send_message() takes a hash; until the peer's identity is
    # known nothing can be built, so each due attempt is a (debounced) path request.
    msg = e.send("hi")
    assert msg is not None and msg.state == LXMessage.OUTBOUND and e.req_times() == [0.0]
    e.advance_to(8)
    assert msg.delivery_attempts == 1 and not e.sent
    lxmf.Identity.known[e.peer.hash] = e.peer
    e.set_path(True)
    e.advance_to(12)
    assert len(e.sent) == 1 and msg.state == LXMessage.SENT


@_with_env(identity_known=False, with_path=False)
def test_unknown_identity_fails_after_the_attempts_via_the_failed_callback(e):
    msg = e.send("hi")
    e.advance_to(200)
    assert msg.state == LXMessage.FAILED and len(e.ev("failed")) == 1 and not e.sent
    # path requests: pre-emptive at t=0, then one per due attempt, 8 s apart (ticks): count 1 + 6
    assert len(e.req_times()) == 7


@_with_env()
def test_callback_errors_do_not_break_processing(e):
    def boom(m):
        raise RuntimeError("ui bug")
    e.router.register_outbound_callbacks(delivered=boom, failed=boom, sent=boom)
    msg = e.send("x")
    msg._mark_delivered()
    e.router.process_outbound()
    assert msg.state == LXMessage.DELIVERED and msg not in e.router.pending_outbound


@_with_env()
def test_router_job_loop_runs_the_queue_inside_an_event_loop(e):
    import asyncio
    saved = LXMRouter.PROCESSING_INTERVAL
    LXMRouter.PROCESSING_INTERVAL = 0.01

    async def main():
        msg = e.send("loop")
        assert e.router._job_task is not None and e.sent_times() == [0.0]
        e.t[0] += 12                       # retry wait over: the job (not a manual call) resends
        await asyncio.sleep(0.1)
        e.router._job_task.cancel()
        return msg
    try:
        msg = asyncio.run(main())
    finally:
        LXMRouter.PROCESSING_INTERVAL = saved
    assert e.sent_times()[:2] == [0.0, 12.0] and msg.delivery_attempts >= 2


# --------------------------------- real Link / Resource plumbing (unit) ----

def test_outgoing_link_close_concludes_in_flight_outgoing_resources():
    ol = object.__new__(link_mod.OutgoingLink)
    ol.status = link_mod.OutgoingLink.ACTIVE
    ol.link_id = b"\x05" * 16
    ol.pending_requests = {}
    ol.closed_callback = None
    ol._channel = None
    ol.incoming_resources = []
    res = object.__new__(resource_mod.Resource)
    res.status = resource_mod.TRANSFERRING
    res.link = ol
    ol.outgoing_resources = [res]
    seen = []
    ol.resource_concluded_callback = lambda r: seen.append(r.status)
    ol._close()
    assert seen == [resource_mod.FAILED] and ol.status == link_mod.OutgoingLink.CLOSED


def test_receiver_cancel_of_an_outgoing_resource_is_a_rejection():
    ol = object.__new__(link_mod.OutgoingLink)
    ol.pending_requests = {}
    res = object.__new__(resource_mod.Resource)
    res.status = resource_mod.ADVERTISED
    res.hash = b"\x09" * 32
    res.link = ol
    res.request_id = None
    ol.outgoing_resources = [res]
    ol.incoming_resources = []
    seen = []
    ol.resource_concluded_callback = lambda r: seen.append(r.status)
    ol._handle_resource_cancel(res.hash, False)                     # initiator-cancel (ICL): plain failure
    assert seen == [resource_mod.FAILED]
    res.status = resource_mod.ADVERTISED
    ol.outgoing_resources = [res]
    ol._handle_resource_cancel(res.hash, True)                      # receiver-cancel (RCL): rejected
    assert seen == [resource_mod.FAILED, resource_mod.REJECTED]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("ok " + t.__name__)
        passed += 1
    print("\n%d passed" % passed)
