# Pure25519 EdDSA (MIT License - Brian Warner)
# Adapted for µReticulum

import os
import binascii
from ..hashes import sha512
from .basic import (bytes_to_clamped_scalar,
                    bytes_to_scalar, scalar_to_bytes,
                    bytes_to_element, bytes_to_element_unchecked,
                    Base, _rev, scalarmult_base_comb)

def H(m):
    return sha512(m)

def publickey(seed):
    assert len(seed) == 32
    a = bytes_to_clamped_scalar(H(seed)[:32])
    A = scalarmult_base_comb(a)
    return A.to_bytes()

def Hint(m):
    h = H(m)
    return int(binascii.hexlify(_rev(h)), 16)

def signature(m, sk, pk):
    assert len(sk) == 32
    assert len(pk) == 32
    h = H(sk[:32])
    a_bytes, inter = h[:32], h[32:]
    a = bytes_to_clamped_scalar(a_bytes)
    r = Hint(inter + m)
    R = scalarmult_base_comb(r)
    R_bytes = R.to_bytes()
    S = r + Hint(R_bytes + pk + m) * a
    return R_bytes + scalar_to_bytes(S)

def signature_cached(m, a, inter, pk):
    """Sign with pre-derived key material (avoids SHA-512 + scalarmult per call).

    Args:
        m: message bytes
        a: pre-derived clamped scalar (from H(seed)[:32])
        inter: pre-derived nonce material (H(seed)[32:])
        pk: pre-computed public key bytes
    """
    r = Hint(inter + m)
    R = scalarmult_base_comb(r)
    R_bytes = R.to_bytes()
    S = r + Hint(R_bytes + pk + m) * a
    return R_bytes + scalar_to_bytes(S)

def checkvalid(s, m, pk):
    if len(s) != 64:
        raise Exception("signature length is wrong")
    if len(pk) != 32:
        raise Exception("public-key length is wrong")
    R = bytes_to_element_unchecked(s[:32])
    A = bytes_to_element_unchecked(pk)
    S = bytes_to_scalar(s[32:])
    h = Hint(s[:32] + pk + m)
    v1 = scalarmult_base_comb(S)
    v2 = R.add(A.scalarmult(h))
    return v1 == v2

def create_signing_key():
    return os.urandom(32)

def create_verifying_key(signing_key):
    return publickey(signing_key)

def sign(skbytes, msg):
    if len(skbytes) != 32:
        raise ValueError("Bad signing key length %d" % len(skbytes))
    vkbytes = create_verifying_key(skbytes)
    sig = signature(msg, skbytes, vkbytes)
    return sig

def verify(vkbytes, sig, msg):
    if len(vkbytes) != 32:
        raise ValueError("Bad verifying key length %d" % len(vkbytes))
    if len(sig) != 64:
        raise ValueError("Bad signature length %d" % len(sig))
    rc = checkvalid(sig, msg, vkbytes)
    if not rc:
        raise ValueError("rc != 0", rc)
    return True
