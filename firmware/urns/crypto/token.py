# µReticulum Token
# Slightly modified Fernet implementation (no VERSION/TIMESTAMP fields)

import os
from .hmac import new as hmac_new
from .pkcs7 import PKCS7
from .aes import AES_128_CBC, AES_256_CBC, AES


class Token:
    TOKEN_OVERHEAD = 48  # bytes

    @staticmethod
    def generate_key(mode=None):
        if mode is None:
            mode = AES_256_CBC
        if mode == AES_128_CBC:
            return os.urandom(32)
        elif mode == AES_256_CBC:
            return os.urandom(64)
        else:
            raise TypeError("Invalid token mode")

    def __init__(self, key=None, mode=AES):
        if key is None:
            raise ValueError("Token key cannot be None")

        if mode == AES:
            if len(key) == 32:
                self.mode = AES_128_CBC
                self._signing_key = key[:16]
                self._encryption_key = key[16:]
            elif len(key) == 64:
                self.mode = AES_256_CBC
                self._signing_key = key[:32]
                self._encryption_key = key[32:]
            else:
                raise ValueError("Token key must be 128 or 256 bits, not " + str(len(key) * 8))
        else:
            raise TypeError("Invalid token mode")

    def verify_hmac(self, token):
        if len(token) <= 32:
            raise ValueError("Cannot verify HMAC on token of only " + str(len(token)) + " bytes")
        received_hmac = token[-32:]
        expected_hmac = hmac_new(self._signing_key, token[:-32]).digest()
        if received_hmac == expected_hmac:
            return True
        return False

    def encrypt(self, data=None):
        if not isinstance(data, bytes):
            raise TypeError("Token plaintext input must be bytes")
        iv = os.urandom(16)
        ciphertext = self.mode.encrypt(
            plaintext=PKCS7.pad(data),
            key=self._encryption_key,
            iv=iv)
        signed_parts = iv + ciphertext
        return signed_parts + hmac_new(self._signing_key, signed_parts).digest()

    def decrypt(self, token=None):
        if not isinstance(token, bytes):
            raise TypeError("Token must be bytes")
        if not self.verify_hmac(token):
            raise ValueError("Token HMAC was invalid")

        iv = token[:16]
        ciphertext = token[16:-32]

        try:
            return PKCS7.unpad(
                self.mode.decrypt(
                    ciphertext=ciphertext,
                    key=self._encryption_key,
                    iv=iv))
        except Exception as e:
            raise ValueError("Could not decrypt token")
