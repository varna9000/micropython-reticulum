# µReticulum Ed25519
# High-level Ed25519 signing and verification
#
# Automatically uses native C module (ed25519_fast) if available
# for the current architecture, otherwise falls back to pure Python.

import os
import sys

_native = None
_note = None

# Try to load native C module (Monocypher Ed25519 with SHA-512, RFC 8032 compatible)
def _try_native():
    global _native, _note
    # 1. IRAM natmod from the filesystem (lib/ed25519_iram.mpy). A dynamically
    #    loaded natmod executes from IRAM; a built-in C module executes from
    #    flash XIP, which on ESP32-S3 measures ~80x slower for this code (the
    #    16KB icache + shared dcache thrash between Monocypher's inner loops
    #    and the PSRAM heap: verify 1461ms XIP vs 17.6ms IRAM). The distinct
    #    module name matters — a VFS .mpy can never shadow a registered
    #    built-in of the same name.
    #    MemoryError (IRAM exhausted) and ValueError (mpy ABI mismatch) fall
    #    through to the built-in, so a bad or missing file only costs speed.
    # Several candidates can be present at once (a stale ed25519_iram.mpy next
    # to a rebuilt ed25519_fast_xtensawin.mpy is a real deployment mistake), so
    # collect all that import and prefer one that has the stamp functions.
    found = []
    try:
        import ed25519_iram
        found.append(ed25519_iram)
    except (ImportError, MemoryError, ValueError, OSError):
        pass
    # 2. Built-in / platform C modules (flash XIP on esp32 - slow but present)
    names = ("ed25519_fast_xtensawin",) if sys.platform == "esp32" else (
        ("ed25519_fast_armv6m",) if sys.platform == "rp2" else ("ed25519_fast",))
    for n in names + ("ed25519_fast",):
        if found and hasattr(found[0], "hashcash") and hasattr(found[0], "x25519_stamp2"):
            break  # already have a current one; do not load more
        try:
            m = __import__(n)
        except (ImportError, MemoryError, ValueError, OSError):
            continue
        if m not in found:
            found.append(m)
    mod = found[0] if found else None
    for m in found:
        if hasattr(m, "hashcash") and hasattr(m, "x25519_stamp2"):
            if m is not mod:
                _note = "stale native module skipped, using a newer one"
            mod = m
            break
    _native = mod

_try_native()


def have_native():
    return _native is not None


def hashcash_module():
    """The native module when it has hashcash() (newer build), else None. The
    one place that probes for it; cheap, and importable without pulling in
    the stamp modules (stamps.have_hashcash and the boot code use it)."""
    nm = _native
    return nm if (nm is not None and hasattr(nm, "hashcash")) else None


if _native:
    from ..log import log, LOG_VERBOSE
    log("Ed25519/X25519: native C module loaded", LOG_VERBOSE)
    if _note:
        log("Ed25519/X25519: " + _note, LOG_VERBOSE)


class Ed25519PrivateKey:
    def __init__(self, seed):
        self.seed = seed
        if _native:
            self._pk = _native.publickey(seed)
        else:
            from .pure25519 import ed25519_oop as ed25519
            self.sk = ed25519.SigningKey(seed)

    @classmethod
    def generate(cls):
        return cls.from_private_bytes(os.urandom(32))

    @classmethod
    def from_private_bytes(cls, data):
        return cls(seed=data)

    def private_bytes(self):
        return self.seed

    def public_key(self):
        if _native:
            return Ed25519PublicKey.from_public_bytes(self._pk)
        return Ed25519PublicKey.from_public_bytes(self.sk.vk_s)

    def sign(self, message):
        if _native:
            return _native.sign(message, self.seed)
        return self.sk.sign(message)


class Ed25519PublicKey:
    def __init__(self, data):
        self._data = data
        if not _native:
            from .pure25519 import ed25519_oop as ed25519
            self.vk = ed25519.VerifyingKey(data)

    @classmethod
    def from_public_bytes(cls, data):
        return cls(data)

    def public_bytes(self):
        return self._data

    def verify(self, signature, message):
        if _native:
            if not _native.verify(signature, message, self._data):
                raise Exception("Ed25519 signature verification failed")
        else:
            self.vk.verify(signature, message)
