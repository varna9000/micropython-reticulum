# µReticulum Hashes
# SHA-256: uses built-in uhashlib (hardware-accelerated on RP2040)
# SHA-512: uses pure-Python fallback or mip hashlib-sha512 package

try:
    import hashlib
    _has_hashlib_sha256 = hasattr(hashlib, 'sha256')
    _has_hashlib_sha512 = hasattr(hashlib, 'sha512')
except ImportError:
    _has_hashlib_sha256 = False
    _has_hashlib_sha512 = False

if not _has_hashlib_sha256:
    try:
        from uhashlib import sha256 as _sha256_cls
        _has_hashlib_sha256 = True
    except ImportError:
        raise RuntimeError("No SHA-256 implementation available")
else:
    _sha256_cls = hashlib.sha256

if _has_hashlib_sha512:
    _sha512_cls = hashlib.sha512
else:
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
