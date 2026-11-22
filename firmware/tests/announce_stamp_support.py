# Helpers for test_announce_stamps.py: a REAL Destination/Identity (the harness
# injects fakes for both) with a fixed test identity.
import importlib.util
import os
import sys

import harness  # noqa: F401
from harness import const, packet, Transport

FW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, relpath, swap=None):
    saved = {}
    for k, v in (swap or {}).items():
        saved[k] = sys.modules.get(k)
        sys.modules[k] = v
    try:
        spec = importlib.util.spec_from_file_location(name, os.path.join(FW, relpath))
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


if "urns.identity_real" in sys.modules:
    ident_mod = sys.modules["urns.identity_real"]
else:
    ident_mod = _load("urns.identity_real", "urns/identity.py")
RealIdentity = ident_mod.Identity
# destination.py binds `Identity` at import: give it the real one.
dest_mod = _load("urns.destination_real", "urns/destination.py", {"urns.identity": ident_mod})
RealDestination = dest_mod.Destination
announce_stamps = _load("urns.announce_stamps", "urns/announce_stamps.py")
# announce_stamps does `from . import announce_stamps` inside Destination.announce
import urns  # noqa: E402
urns.announce_stamps = announce_stamps
stamps = importlib_stamps = sys.modules.get("urns.stamps") or __import__("importlib").import_module("urns.stamps")

# Fixed test identity: X25519 prv (32) + Ed25519 seed (32)
TEST_PRV = bytes.fromhex(
    "a0b1c2d3e4f5061728394a5b6c7d8e9fa0b1c2d3e4f5061728394a5b6c7d8e0f"
    "00112233445566778899aabbccddeeff102132435465768798a9bacbdcedfe0f")


def make_identity():
    i = RealIdentity(create_keys=False)
    assert i.load_private_key(TEST_PRV)
    return i


def make_destination(identity=None, app="lxmf", aspect="delivery"):
    identity = identity or make_identity()
    Transport.destinations = []
    return RealDestination(identity, RealDestination.IN, RealDestination.SINGLE, app, aspect)


def lxmf_app_data(name="Test Node"):
    import importlib
    um = importlib.import_module("urns.umsgpack")
    return um.packb([name.encode(), None, [0]])
