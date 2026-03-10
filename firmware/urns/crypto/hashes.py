# µReticulum Hashes
# SHA-256: uses uhashlib (hardware-accelerated on ESP32)
# SHA-512: uses pure-Python fallback (uhashlib has no sha512)

from uhashlib import sha256 as _sha256_cls
from .sha512 import sha512 as _sha512_cls


def sha256(data):
    h = _sha256_cls()
    h.update(data)
    return h.digest()


def sha512(data):
    h = _sha512_cls()
    h.update(data)
    return h.digest()


def sha256_hasher():
    return _sha256_cls()


def sha512_hasher():
    return _sha512_cls()
