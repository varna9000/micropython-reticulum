# Resource overall-timeout tests (host-side, crypto-free). Run:
#   python3 firmware/tests/test_resource_timeout.py
#
# urns used a flat 120 s ceiling (is_timed_out), and it was only ever checked
# for OUTGOING resources — so a receiver whose parts merely trickle (every
# received part refunds the retry budget) had NO overall ceiling and hung
# until the app's own cap. Reference RNS has no fixed ceiling; it scales the
# give-up with link RTT. These tests pin:
#   1. is_timed_out() scales with link.rtt (clamped to a floor and a cap), and
#   2. a stalled INCOMING resource is concluded (and its request failed) once
#      that ceiling passes, via check_request_timeout.

import time
import types
import importlib

import harness
from harness import const, packet, Transport, MockInterface, reset_transport, link

resource_mod = importlib.import_module("urns.resource")
Resource = resource_mod.Resource

LINK_ID = b"\x30" * 16
DEST = b"\xE1" * 16

_failures = []


def check(cond, name, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "  ->  " + detail))
    if not cond:
        _failures.append(name)


class _PassToken:
    def encrypt(self, d):
        return bytes(d)

    def decrypt(self, d):
        return bytes(d)


def _res(rtt, age_s):
    """Bare resource carrying only what is_timed_out() reads."""
    r = object.__new__(Resource)
    r.link = types.SimpleNamespace(rtt=rtt)
    r.created_at = time.time() - age_s
    return r


def _mklink():
    OL = link.OutgoingLink
    ol = object.__new__(OL)
    ol.status = OL.ACTIVE
    ol.link_id = LINK_ID
    ol.hash = LINK_ID
    ol._token = _PassToken()
    ol.rtt = 3.0
    ol.pending_requests = {}
    ol.incoming_resources = []
    ol.outgoing_resources = []
    ol.resource_concluded_callback = None
    return ol


def _rx_resource(ol, age_s, request_id):
    """A receiver-side resource mid-transfer, created age_s ago."""
    r = object.__new__(Resource)
    r.link = ol
    r.is_initiator = False
    r.status = resource_mod.TRANSFERRING
    r.hash = b"\x99" * 32
    r.request_id = request_id
    r.created_at = time.time() - age_s
    r.last_request_at = 0
    r.last_part_at = 0
    ol.incoming_resources.append(r)
    return r


# --------------------------------------------------------------------------
# is_timed_out() scales with RTT (not a flat 120 s)
# --------------------------------------------------------------------------

def test_slow_link_still_alive_past_120s():
    # LoRa-ish rtt=3s -> ceiling well past 120s, so a transfer 150s in lives.
    check(_res(3.0, 150).is_timed_out() is False,
          "slow link (rtt=3s) still transferring at 150s (was flat 120s)")


def test_slow_link_is_still_bounded():
    check(_res(3.0, 400).is_timed_out() is True,
          "slow link still gives up eventually (400s > scaled ceiling)")


def test_huge_rtt_is_clamped_to_cap():
    check(_res(1000.0, 500).is_timed_out() is False,
          "pathological rtt is capped, not unbounded — alive at 500s")
    check(_res(1000.0, 620).is_timed_out() is True,
          "capped ceiling still fires past ~600s")


def test_fast_link_keeps_a_floor():
    check(_res(0.01, 60).is_timed_out() is False,
          "fast link keeps at least the old floor — alive at 60s")
    check(_res(0.0, 200).is_timed_out() is True,
          "unknown rtt falls back to the floor and fires past it")


# --------------------------------------------------------------------------
# The receiver now has an overall ceiling (check_request_timeout)
# --------------------------------------------------------------------------

def test_stalled_receiver_times_out_and_fails_request():
    reset_transport()
    mi = MockInterface("m")
    Transport.interfaces.append(mi)
    ol = _mklink()
    rid = b"\x08" * 16
    failed = []
    ol.pending_requests[rid] = [
        link.OutgoingLink.REQ_RECEIVING, time.time(), 30,
        None, lambda r_id: failed.append(r_id), None, None,
    ]
    r = _rx_resource(ol, age_s=700, request_id=rid)   # well past the ceiling

    r.check_request_timeout()

    check(r.status == resource_mod.FAILED,
          "a stalled incoming resource is cancelled once its ceiling passes",
          "status=%r" % r.status)
    check(failed == [rid],
          "and the pending request is failed (app hears it, not a silent hang)",
          "failed=%r" % (failed,))


def test_progressing_receiver_not_timed_out_early():
    reset_transport()
    mi = MockInterface("m")
    Transport.interfaces.append(mi)
    ol = _mklink()
    rid = b"\x09" * 16
    ol.pending_requests[rid] = [
        link.OutgoingLink.REQ_RECEIVING, time.time(), 30, None, None, None, None,
    ]
    # rtt=3 -> ceiling ~366s; a transfer 120s in must NOT be abandoned.
    r = _rx_resource(ol, age_s=120, request_id=rid)
    r.check_request_timeout()
    check(r.status == resource_mod.TRANSFERRING,
          "a receiver 120s into a slow-link transfer keeps going",
          "status=%r" % r.status)


if __name__ == "__main__":
    for _k, _v in sorted(globals().items()):
        if _k.startswith("test_"):
            _v()
    print("\n%d checks failed" % len(_failures) if _failures
          else "\nall resource-timeout tests passed")
    raise SystemExit(1 if _failures else 0)
