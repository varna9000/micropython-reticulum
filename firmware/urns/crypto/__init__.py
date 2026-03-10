# µReticulum Cryptography
# Always uses internal provider (no PyCA/OpenSSL on MicroPython)

from .hashes import sha256, sha512
from .hkdf import hkdf
from .pkcs7 import PKCS7
from .token import Token
from .x25519 import X25519PrivateKey, X25519PublicKey
from .ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PROVIDER_INTERNAL = 0x01
PROVIDER = PROVIDER_INTERNAL

def backend():
    return "internal"
