# Constant-time HMAC comparison (host-side).
#
# RNS 1.5.0 hardens Token HMAC verification to a constant-time comparison
# (crypto/hmac.py compare_digest, used by Token.verify_hmac) instead of a plain
# ``==`` that short-circuits on the first differing byte. These tests cover the
# comparator directly and its integration into Token.verify_hmac (which needs
# only SHA-256, not AES, so it runs crypto-lightly on the host).
#
# Run:  python3 firmware/tests/test_token_hmac.py

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401  (installs MicroPython shims + synthetic urns pkg)
import importlib

hmac = importlib.import_module("urns.crypto.hmac")
compare_digest = hmac.compare_digest


def test_equal():
    assert compare_digest(bytes(range(32)), bytes(range(32))) is True


def test_empty_equal():
    assert compare_digest(b"", b"") is True


def test_unequal_first_byte():
    a = bytes(32)
    b = bytearray(32)
    b[0] = 1
    assert compare_digest(a, bytes(b)) is False


def test_unequal_last_byte():
    # No early exit: a difference in the final byte is still detected.
    a = bytes(32)
    b = bytearray(32)
    b[31] = 1
    assert compare_digest(a, bytes(b)) is False


def test_length_mismatch():
    assert compare_digest(b"", b"\x00") is False
    assert compare_digest(b"abc", b"abcd") is False


def test_token_verify_hmac_integration():
    token_mod = importlib.import_module("urns.crypto.token")
    Token = token_mod.Token
    key = bytes(range(32))                 # 32B key -> signing_key = key[:16]
    tok = Token(key)
    signing_key = key[:16]
    payload = b"the quick brown fox jumps over the lazy dog"
    good = hmac.new(signing_key, payload).digest()
    token = payload + good
    assert tok.verify_hmac(token) is True
    # Flip a byte of the HMAC -> reject.
    bad = bytearray(token)
    bad[-1] ^= 0x01
    assert tok.verify_hmac(bytes(bad)) is False
    # Flip a byte of the payload -> recomputed HMAC mismatches -> reject.
    bad2 = bytearray(token)
    bad2[0] ^= 0x01
    assert tok.verify_hmac(bytes(bad2)) is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("ok " + t.__name__)
        passed += 1
    print("\n%d/%d passed" % (passed, len(tests)))
