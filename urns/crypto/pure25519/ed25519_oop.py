# Pure25519 Ed25519 OOP interface (MIT License - Brian Warner)
# Adapted for µReticulum: stripped base64 encoding, simplified

import os
from . import _ed25519

BadSignatureError = _ed25519.BadSignatureError


def create_keypair(entropy=os.urandom):
    SEEDLEN = 32
    seed = entropy(SEEDLEN)
    sk = SigningKey(seed)
    vk = sk.get_verifying_key()
    return sk, vk


class SigningKey:
    def __init__(self, sk_s):
        assert isinstance(sk_s, (bytes, bytearray))
        if len(sk_s) == 32:
            vk_s, sk_s = _ed25519.publickey(sk_s)
        else:
            if len(sk_s) != 64:
                raise ValueError("SigningKey takes 32-byte seed or 64-byte string")
        self.sk_s = sk_s  # seed+pubkey
        self.vk_s = sk_s[32:]  # just pubkey

        # P0a: Pre-derive key material to avoid SHA-512 + clamping per sign
        from ..hashes import sha512
        from .basic import bytes_to_clamped_scalar
        h = sha512(self.sk_s[:32])
        self._cached_a = bytes_to_clamped_scalar(h[:32])
        self._cached_inter = h[32:]

    def to_bytes(self):
        return self.sk_s

    def to_seed(self):
        return self.sk_s[:32]

    def __eq__(self, them):
        if not isinstance(them, SigningKey):
            return False
        return them.sk_s == self.sk_s

    def get_verifying_key(self):
        return VerifyingKey(self.vk_s)

    def sign(self, msg):
        assert isinstance(msg, (bytes, bytearray))
        # P0a: Use cached key derivation - avoids H(seed) + clamping per sign
        from . import eddsa
        sig = eddsa.signature_cached(msg, self._cached_a, self._cached_inter, self.vk_s)
        sig_R = sig[0:32]
        sig_S = sig[32:64]
        return sig_R + sig_S


class VerifyingKey:
    def __init__(self, vk_s):
        if not isinstance(vk_s, (bytes, bytearray)):
            raise TypeError("VerifyingKey requires bytes")
        assert len(vk_s) == 32
        self.vk_s = vk_s

    def to_bytes(self):
        return self.vk_s

    def __eq__(self, them):
        if not isinstance(them, VerifyingKey):
            return False
        return them.vk_s == self.vk_s

    def verify(self, sig, msg):
        assert isinstance(sig, (bytes, bytearray))
        assert isinstance(msg, (bytes, bytearray))
        assert len(sig) == 64
        sig_R = sig[:32]
        sig_S = sig[32:]
        sig_and_msg = sig_R + sig_S + msg
        msg2 = _ed25519.open(sig_and_msg, self.vk_s)
        assert msg2 == msg
