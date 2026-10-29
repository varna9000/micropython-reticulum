# Byte-level IFAC interop against the reference Reticulum implementation:
# the same network name + passphrase must give the same key, urns' signed
# frame must be what RNS.Transport.handle_outgoing_ifac produces, the
# reference must accept urns' frame and urns must accept the reference's.
#
# Needs the `rns` package (pip install rns); skips cleanly without it, so the
# plain host suite stays dependency-free. Run with a venv that has it:
#   .../venv/bin/python firmware/tests/test_ifac_reference.py

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401

try:
    import RNS
    import RNS.Transport
    import RNS.Cryptography
except ImportError:
    print("SKIP  reference RNS not installed (pip install rns)")
    sys.exit(0)

from urns.interfaces import Interface  # noqa: E402
from urns.transport import Transport  # noqa: E402

NAME, PASS = "mymesh", "correct horse"
PKT = bytes([0x00, 0x01]) + bytes(range(16)) + b"payload-bytes" * 7


class RefIface:
    """What the reference's IFAC helpers read off an interface, derived the
    way Reticulum.interface_post_init does it."""

    def __init__(self, netname, netkey, size):
        origin = b""
        if netname is not None:
            origin += RNS.Identity.full_hash(netname.encode("utf-8"))
        if netkey is not None:
            origin += RNS.Identity.full_hash(netkey.encode("utf-8"))
        self.ifac_key = RNS.Cryptography.hkdf(length=64, derive_from=RNS.Identity.full_hash(origin),
                                              salt=RNS.Reticulum.IFAC_SALT, context=None)
        self.ifac_identity = RNS.Identity.from_bytes(self.ifac_key)
        self.ifac_size = size
        self.violations = 0

    def ifac_violation(self, msg):
        self.violations += 1


def urns_iface(size, netname=NAME, netkey=PASS):
    i = Interface("t")
    i.setup_ifac({"networkname": netname, "passphrase": netkey, "ifac_size": size})
    return i


def test_same_key_from_name_and_passphrase():
    assert urns_iface(16).ifac_key == RefIface(NAME, PASS, 16).ifac_key


def test_same_key_from_name_alone():
    assert urns_iface(16, netkey=None).ifac_key == RefIface(NAME, None, 16).ifac_key


def test_urns_frame_is_byte_identical_to_reference_16():
    assert urns_iface(16).ifac_sign(PKT) == RNS.Transport.handle_outgoing_ifac(RefIface(NAME, PASS, 16), PKT)


def test_urns_frame_is_byte_identical_to_reference_8():
    assert urns_iface(8).ifac_sign(PKT) == RNS.Transport.handle_outgoing_ifac(RefIface(NAME, PASS, 8), PKT)


def test_reference_accepts_urns_frame():
    ref = RefIface(NAME, PASS, 16)
    ok, raw = RNS.Transport.handle_ifac(urns_iface(16).ifac_sign(PKT), ref)
    assert ok and raw == PKT and ref.violations == 0


def test_urns_accepts_reference_frame():
    wire = RNS.Transport.handle_outgoing_ifac(RefIface(NAME, PASS, 8), PKT)
    assert Transport._ifac_validate(wire, urns_iface(8)) == PKT


def test_reference_rejects_wrong_passphrase():
    ref = RefIface(NAME, "wrong", 16)
    ok, raw = RNS.Transport.handle_ifac(urns_iface(16).ifac_sign(PKT), ref)
    assert not ok and ref.violations == 1


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
    print("\n%d/%d passed (reference RNS %s)" % (len(tests) - failed, len(tests), RNS.__version__))
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
