# Inbound link-request key stamp (Reticulum 1.6 Link.validate_request), review finding 4.
# Both ends REAL (ownlink_harness): A's OutgoingLink searches a stamped key, B's inbound
# Link validates it before any ECDH. Integer-second wall clock. Run:
#   python3 firmware/tests/test_link_request_stamps_inbound.py
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ownlink_harness as H
from ownlink_harness import const, Identity, link_mod, lxmf
from test_own_link_proof import World
from urns.crypto import X25519PrivateKey

LXMessage = lxmf.LXMessage


class _Env:
    def __init__(self, a_cost, b_cost):
        self.real_time = time.time
        t0 = int(self.real_time())
        self.base = self.real_time()
        time.time = lambda: t0 + int(self.real_time() - self.base)       # integer seconds
        self.real_recall = Identity.recall_link_cost
        self.real_gen = X25519PrivateKey.generate
        self.gens = 0
        env = self

        def counting(*a, **k):
            env.gens += 1
            return env.real_gen(*a, **k)
        X25519PrivateKey.generate = staticmethod(counting)
        self.w = World()
        self.w.b_dest.link_cost = b_cost
        bh = self.w.b_dest.hash
        Identity.recall_link_cost = staticmethod(lambda h: a_cost if h == bh else None)

    def close(self):
        time.time = self.real_time
        Identity.recall_link_cost = self.real_recall
        X25519PrivateKey.generate = self.real_gen


def _send(env):
    w = env.w
    with w.a.ctx():
        w.router.send_message(w.b_dest.hash, "hi", desired_method=LXMessage.DIRECT)
    env.gens = 0                                  # A's own key work is done; count only B's
    w.pump()


def _run(a_cost, b_cost):
    env = _Env(a_cost, b_cost)
    try:
        _send(env)
        return len(env.w.b_links), env.gens
    finally:
        env.close()


def test_stamped_request_from_our_own_outgoing_link_is_accepted():
    for _ in range(100):                   # the key search is random: 100 independent requests
        links, gens = _run(3.0, 3.0)
        assert links == 1 and gens >= 1, (links, gens)


def test_unstamped_request_is_ignored_and_costs_no_ecdh():
    for _ in range(20):                    # random keys: 20 tries, cost 16 -> never passes by luck
        links, gens = _run(None, 16.0)
        assert links == 0 and gens == 0, (links, gens)


def test_cost_zero_or_unset_accepts_everything_as_before():
    for cost in (None, 0, 0.0):
        links, gens = _run(None, cost)
        assert links == 1 and gens >= 1, (cost, links, gens)


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
