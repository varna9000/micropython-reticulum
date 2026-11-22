# Host test: the ESP32 native module must be the same current build under every
# name the loader or the docs use, and carry the stamp symbols; and the loader
# prefers a module that has them over a stale one that imported first.
import os, sys, hashlib, types
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import harness  # shims + synthetic urns package
import importlib

NEED = (b"hashcash", b"x25519_stamp2", b"x25519_stamp2_init")
FILES = [
    "firmware/lib/ed25519_fast_xtensawin.mpy",
    "firmware/lib/ed25519_iram.mpy",
    "tools/natmod/ed25519_fast/ed25519_fast_xtensawin.mpy",
]

def md5(p):
    return hashlib.md5(open(os.path.join(ROOT, p), "rb").read()).hexdigest()

def check_files():
    sums = {f: md5(f) for f in FILES}
    assert len(set(sums.values())) == 1, "ESP32 native module copies differ: %r" % sums
    data = open(os.path.join(ROOT, FILES[0]), "rb").read()
    for sym in NEED:
        assert sym in data, "missing symbol %r" % sym
    return sums

def check_loader():
    # Simulate the device: ed25519_iram stale, ed25519_fast_xtensawin current.
    stale = types.ModuleType("ed25519_iram")
    stale.publickey = stale.sign = stale.verify = lambda *a: None
    cur = types.ModuleType("ed25519_fast_xtensawin")
    cur.publickey = cur.sign = cur.verify = lambda *a: None
    cur.hashcash = cur.x25519_stamp2 = cur.x25519_stamp2_init = lambda *a: None
    for order in (("stale-first",), ("cur-only",)):
        sys.modules.pop("urns.crypto.ed25519", None)
        sys.modules["ed25519_iram"] = stale if order[0] == "stale-first" else cur
        sys.modules["ed25519_fast_xtensawin"] = cur
        real = sys.platform
        sys.platform = "esp32"
        try:
            ed25519 = importlib.import_module("urns.crypto.ed25519")
        finally:
            sys.platform = real
        assert ed25519._native is cur, order
        assert ed25519.hashcash_module() is cur
        if order[0] == "stale-first":
            assert ed25519._note
    for n in ("ed25519_iram", "ed25519_fast_xtensawin"):
        sys.modules.pop(n, None)

if __name__ == "__main__":
    check_files()
    check_loader()
    print("test_native_module_files OK")
