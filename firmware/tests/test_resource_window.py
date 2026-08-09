# Adaptive Resource windowing + RTT-scaled timer tests (host-side, crypto-free).
#
# Drives real sender-side and receiver-side Resource objects over stub links
# with a controllable millisecond clock (resource._ticks_ms is monkeypatched),
# covering: window growth per completed round, the fast unlock (window_max 75),
# the very-slow clamp (window_max 4), the slow-init clamp (link.rtt > 1.45),
# shrink-on-timeout with exact reference nesting, the retry budget (spent on
# timeouts, refunded by parts — the old per-request accounting leaked), the
# mdu-based request fit bound, zero-elapsed rounds (integer device clocks),
# and the RTT-scaled advertisement retry.
#
# Run:  python3 firmware/tests/test_resource_window.py

import sys
import os
import time
import importlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
from harness import Transport, MockInterface, reset_transport

resource_mod = importlib.import_module("urns.resource")
Resource = resource_mod.Resource

LINK_ID = b"\x30" * 16

_REAL_TICKS = resource_mod._ticks_ms
_REAL_DIFF = resource_mod._ticks_diff


class _PassToken:
    def encrypt(self, data):
        return bytes(data)

    def decrypt(self, data):
        return bytes(data)


class _StubLink:
    ACTIVE = 0x01

    def __init__(self, rtt=0.05):
        self.status = 0x01
        self.link_id = LINK_ID
        self.rtt = rtt
        self.mtu = 500
        self.sdu = 464
        self.mdu = 431
        self._token = _PassToken()
        self.sent = []   # (data, context)
        self.incoming_resources = []
        self.outgoing_resources = []

    def send(self, data, context=None):
        self.sent.append((bytes(data), context))

    def register_incoming_resource(self, r):
        self.incoming_resources.append(r)

    def register_outgoing_resource(self, r):
        self.outgoing_resources.append(r)

    def resource_concluded(self, r):
        pass


class _Clock:
    def __init__(self):
        self.ms = 1_000_000

    def advance(self, ms):
        self.ms += ms


def _fake_clock():
    clk = _Clock()
    resource_mod._ticks_ms = lambda: clk.ms
    resource_mod._ticks_diff = lambda a, b: a - b
    return clk


def _restore_clock():
    resource_mod._ticks_ms = _REAL_TICKS
    resource_mod._ticks_diff = _REAL_DIFF


def _mkpair(data_len=6000, rx_rtt=0.05):
    """Real sender + receiver Resource joined by the adv bytes."""
    reset_transport()
    Transport.interfaces = [MockInterface("tcp")]
    txl = _StubLink()
    data = os.urandom(data_len)
    snd = Resource(txl, data)
    adv, ctx = txl.sent[0]
    rxl = _StubLink(rtt=rx_rtt)
    rcv = Resource.accept(adv, rxl)
    assert rcv is not None
    return snd, rcv, rxl, data


def _requested_count(req_data):
    """Number of maphashes in a request payload (no-HMU form)."""
    assert req_data[0] == resource_mod.HASHMAP_IS_NOT_EXHAUSTED
    return (len(req_data) - 1 - 32) // resource_mod.MAPHASH_LEN


def _feed_round(snd, rcv, rxl, cursor):
    """Feed exactly the parts the last request asked for; return new cursor."""
    n = _requested_count(rxl.sent[-1][0])
    for i in range(cursor, min(cursor + n, snd.total_parts)):
        rcv.receive_part(snd.parts[i])
    return cursor + n


def test_window_grows_per_round():
    clk = _fake_clock()
    try:
        snd, rcv, rxl, _ = _mkpair()
        assert rcv.window == resource_mod.WINDOW
        assert _requested_count(rxl.sent[-1][0]) == 4
        clk.advance(100)
        cur = _feed_round(snd, rcv, rxl, 0)
        assert rcv.window == 5, rcv.window
        assert _requested_count(rxl.sent[-1][0]) == 5
        clk.advance(100)
        _feed_round(snd, rcv, rxl, cur)
        assert rcv.window == 6
    finally:
        _restore_clock()


def test_fast_rate_unlocks_big_window():
    clk = _fake_clock()
    try:
        snd, rcv, rxl, _ = _mkpair()
        cur = 0
        for _ in range(2):          # 2 samples per round -> threshold 4 in 2 rounds
            clk.advance(100)        # 4x483 B in 0.1 s ≈ 19 kB/s > RATE_FAST
            cur = _feed_round(snd, rcv, rxl, cur)
        assert rcv.fast_rate_rounds == resource_mod.FAST_RATE_THRESHOLD
        assert rcv.window_max == resource_mod.WINDOW_MAX_FAST
    finally:
        _restore_clock()


def test_very_slow_rate_clamps_window():
    clk = _fake_clock()
    try:
        snd, rcv, rxl, _ = _mkpair()
        cur = 0
        for _ in range(2):          # ~193 B/s < RATE_VERY_SLOW, 2 full rounds
            clk.advance(10_000)
            cur = _feed_round(snd, rcv, rxl, cur)
        assert rcv.very_slow_rate_rounds == resource_mod.VERY_SLOW_RATE_THRESHOLD
        assert rcv.window_max == resource_mod.WINDOW_MAX_VERY_SLOW
        assert rcv.fast_rate_rounds == 0
    finally:
        _restore_clock()


def test_slow_init_clamp_on_high_rtt_link():
    snd, rcv, rxl, _ = _mkpair(rx_rtt=2.0)
    assert rcv.window_max == resource_mod.WINDOW_MAX_VERY_SLOW
    assert _requested_count(rxl.sent[-1][0]) == 4


def test_timeout_shrinks_window_and_spends_budget():
    snd, rcv, rxl, _ = _mkpair()
    assert rcv.retries_left == resource_mod.MAX_RETRIES
    rcv.last_request_at = time.time() - 300
    rcv.last_part_at = 0
    rcv.check_request_timeout()
    assert rcv.window == 3, "window shrinks on timeout"
    assert rcv.window_max == 8, "window_max follows with the flexibility rule"
    assert rcv.retries_left == resource_mod.MAX_RETRIES - 1
    assert len(rxl.sent) == 2, "a fresh request must go out"
    # Progress refunds the whole budget.
    rcv.receive_part(snd.parts[0])
    assert rcv.retries_left == resource_mod.MAX_RETRIES


def test_timeout_at_window_min_preserves_window_max():
    snd, rcv, rxl, _ = _mkpair()
    rcv.window = rcv.window_min
    rcv.last_request_at = time.time() - 300
    rcv.last_part_at = 0
    rcv.check_request_timeout()
    assert rcv.window == rcv.window_min
    assert rcv.window_max == resource_mod.WINDOW_MAX_SLOW, \
        "window_max must not erode while window sits at min (reference nesting)"


def test_parts_refresh_timeout_anchor():
    snd, rcv, rxl, _ = _mkpair()
    rcv.last_request_at = time.time() - 300
    rcv.receive_part(snd.parts[0])       # trickling round: anchor refreshed
    before = len(rxl.sent)
    rcv.check_request_timeout()
    assert len(rxl.sent) == before, "no re-request while parts are arriving"
    assert rcv.window == resource_mod.WINDOW


def test_retry_budget_not_leaked_by_successful_rounds():
    clk = _fake_clock()
    try:
        snd, rcv, rxl, _ = _mkpair(data_len=15000)   # many rounds
        cur = 0
        for _ in range(4):    # 5 requests total incl. accept's — old budget of 5 gone
            clk.advance(100)
            cur = _feed_round(snd, rcv, rxl, cur)
        assert rcv.status == resource_mod.TRANSFERRING
        rcv.last_request_at = time.time() - 300
        rcv.last_part_at = 0
        before = len(rxl.sent)
        rcv.check_request_timeout()
        assert rcv.status == resource_mod.TRANSFERRING, \
            "stall after many good rounds must retry, not cancel"
        assert len(rxl.sent) == before + 1
    finally:
        _restore_clock()


def test_request_fit_bound_is_mdu_based():
    snd, rcv, rxl, _ = _mkpair(data_len=15000)
    rcv.window = 75
    rxl.mdu = 60                       # tiny mdu -> (60-34)//4 = 6 hashes max
    rcv.request_next()
    assert _requested_count(rxl.sent[-1][0]) == 6


def test_zero_elapsed_round_is_safe():
    clk = _fake_clock()                # frozen clock: elapsed always 0
    try:
        snd, rcv, rxl, _ = _mkpair()
        cur = _feed_round(snd, rcv, rxl, 0)
        _feed_round(snd, rcv, rxl, cur)
        assert rcv.fast_rate_rounds == 0, "no samples from zero-elapsed rounds"
        assert rcv.eifr == 0.0
        assert rcv.window == 6, "rounds still complete and grow the window"
    finally:
        _restore_clock()


def test_adv_retry_is_rtt_scaled():
    reset_transport()
    Transport.interfaces = [MockInterface("tcp")]
    txl = _StubLink(rtt=0.05)
    snd = Resource(txl, os.urandom(1000))
    snd.last_adv_at = time.time() - 3       # fast link: interval floors at 2 s
    snd.check_adv_timeout()
    assert snd.adv_retries == 1, "3 s silence on a fast link must re-advertise"

    txl2 = _StubLink(rtt=4.0)               # slow link: interval caps at 15 s
    snd2 = Resource(txl2, os.urandom(1000))
    snd2.last_adv_at = time.time() - 3
    snd2.check_adv_timeout()
    assert snd2.adv_retries == 0, "3 s silence on a slow link is not a stall"


def test_sender_measures_rtt_on_first_request():
    reset_transport()
    Transport.interfaces = [MockInterface("tcp")]
    txl = _StubLink()
    snd = Resource(txl, os.urandom(1000))
    snd.last_adv_at = time.time() - 0.5
    req = bytes([resource_mod.HASHMAP_IS_NOT_EXHAUSTED]) + snd.hash + snd.hashmap[0:4]
    snd.handle_request(req)
    assert snd.rtt is not None and snd.rtt > 0
    assert snd.status == resource_mod.TRANSFERRING


def test_full_transfer_completes():
    clk = _fake_clock()
    try:
        snd, rcv, rxl, data = _mkpair(data_len=3000)
        cur = 0
        while rcv.status == resource_mod.TRANSFERRING and cur < snd.total_parts:
            clk.advance(50)
            cur = _feed_round(snd, rcv, rxl, cur)
        assert rcv.status == resource_mod.COMPLETE
        assert rcv.data == data
    finally:
        _restore_clock()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("ok " + t.__name__)
        passed += 1
    print("\n%d/%d passed" % (passed, len(tests)))
