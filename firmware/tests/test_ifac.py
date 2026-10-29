# Host-side tests for Interface Access Codes (IFAC): key derivation in
# Interface.setup_ifac, signing + masking in Interface.ifac_sign, validation
# + stripping in Transport._ifac_validate, and the LoRa interface's size
# ceiling (a 500 B packet + 8 B code is exactly the two-frame limit of 508;
# anything larger cannot be split, which is why the reference RNode default
# is 8 and why this interface refuses more).
#
# Real crypto (pure-Python Ed25519 + HKDF) under CPython; no hardware.
#
# Run:  python3 firmware/tests/test_ifac.py

import sys
import os
import time as _pytime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401  (installs shims + synthetic urns package)

import time
if not hasattr(time, "ticks_ms"):
    time.ticks_ms = lambda: int(_pytime.monotonic() * 1000)
    time.ticks_add = lambda t, d: t + d
    time.ticks_diff = lambda a, b: a - b
    time.sleep_ms = lambda ms: None

from urns.interfaces import Interface  # noqa: E402
from urns.interfaces.lora import LoRaInterface, _FRAME_PAYLOAD  # noqa: E402
from urns.transport import Transport  # noqa: E402

CODE = {"networkname": "mymesh", "passphrase": "correct horse"}
# A plausible wire packet: 2 header bytes + 16 B destination + payload.
PKT = bytes([0x00, 0x01]) + bytes(range(16)) + b"payload-bytes" * 7


def iface(cfg, cls=Interface, **kw):
    i = cls("t") if cls is Interface else cls(dict({"name": "t"}, **kw))
    i.setup_ifac(dict(cfg, **kw))
    return i


# --- round trip -----------------------------------------------------------

def test_round_trip_default_size_16():
    a, b = iface(CODE), iface(CODE)
    wire = a.ifac_sign(PKT)
    assert len(wire) == len(PKT) + 16
    assert wire[0] & 0x80                       # IFAC flag set on the wire
    assert Transport._ifac_validate(wire, b) == PKT


def test_round_trip_size_8():
    a, b = iface(CODE, ifac_size=8), iface(CODE, ifac_size=8)
    wire = a.ifac_sign(PKT)
    assert len(wire) == len(PKT) + 8
    assert Transport._ifac_validate(wire, b) == PKT


def test_wrong_passphrase_dropped():
    a = iface(CODE)
    b = iface({"networkname": "mymesh", "passphrase": "wrong"})
    assert Transport._ifac_validate(a.ifac_sign(PKT), b) is None


def test_size_mismatch_dropped():
    a = iface(CODE, ifac_size=16)
    b = iface(CODE, ifac_size=8)
    assert Transport._ifac_validate(a.ifac_sign(PKT), b) is None


def test_name_alone_is_enough():
    a, b = iface({"networkname": "mymesh"}), iface({"networkname": "mymesh"})
    assert a.ifac_signing_key is not None
    assert Transport._ifac_validate(a.ifac_sign(PKT), b) == PKT


# --- setup_ifac edge cases ---------------------------------------------------

def test_empty_strings_mean_no_ifac():
    # The reference treats "" (and "None") as unset; a deck with blank fields
    # must come up with no access code, not one derived from nothing.
    i = iface({"networkname": "", "passphrase": ""})
    assert i.ifac_signing_key is None
    assert i.ifac_sign(PKT) == PKT


def test_setup_without_code_clears_a_previous_one():
    i = iface(CODE)
    assert i.ifac_signing_key is not None
    i.setup_ifac({})                             # the user removed Net and Pass
    assert i.ifac_signing_key is None
    assert i.ifac_key is None
    assert i.ifac_size == 0
    assert i.ifac_sign(PKT) == PKT


# --- LoRa ceiling ---------------------------------------------------------

def test_lora_clamps_size_to_8():
    i = iface(CODE, cls=LoRaInterface, ifac_size=16)
    assert i.ifac_size == 8


def test_lora_default_size_is_8():
    i = iface(CODE, cls=LoRaInterface)
    assert i.ifac_size == 8


def test_base_interface_keeps_16():
    i = iface(CODE, ifac_size=16)
    assert i.ifac_size == 16


class FakeModem:
    def __init__(self):
        self.sent = []

    def _cmd(self, fmt, *args, n_read=0, **kw):
        return bytes([0x00, 255])                # quiet channel

    def send(self, frame):
        self.sent.append(bytes(frame))

    def start_recv(self, continuous=False):
        pass


def lora_tx(**cfg):
    i = LoRaInterface(dict({"name": "t", "lbt_rssi": None}, **cfg))
    i._modem = FakeModem()
    i.online = True
    i.setup_ifac(cfg)
    return i


def test_lora_full_packet_with_code_fits_two_frames():
    i = lora_tx(**CODE)
    assert i.process_outgoing(bytes([0x00, 0x01]) + b"x" * 498) is True
    assert len(i._modem.sent) == 2
    assert all(len(f) <= _FRAME_PAYLOAD + 1 for f in i._modem.sent)


def test_lora_oversize_after_signing_is_dropped_not_overflowed():
    # 505 B passes a pre-signing check (<= 508) but is 513 B once signed.
    i = lora_tx(**CODE)
    assert i.process_outgoing(bytes([0x00, 0x01]) + b"x" * 503) is False
    assert i._modem.sent == []


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
    sys.exit(1 if _run() else 0)
