# µReticulum Ed25519
# High-level Ed25519 signing and verification
#
# Automatically uses native C module (ed25519_fast) if available
# for the current architecture, otherwise falls back to pure Python.

import os
import sys

_native = None

# Try to load native C module (Monocypher Ed25519 with SHA-512, RFC 8032 compatible)
def _try_native():
    global _native
    mod = None
    try:
        if sys.platform == "esp32":
            import ed25519_fast_xtensawin
            mod = ed25519_fast_xtensawin
        elif sys.platform == "rp2":
            import ed25519_fast_armv6m
            mod = ed25519_fast_armv6m
        else:
            import ed25519_fast
            mod = ed25519_fast
    except ImportError:
        pass
    if mod is None:
        try:
            import ed25519_fast
            mod = ed25519_fast
        except ImportError:
            pass
    _native = mod

_try_native()

if _native:
    from ..log import log, LOG_VERBOSE
    log("Ed25519/X25519: native C module loaded", LOG_VERBOSE)


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
