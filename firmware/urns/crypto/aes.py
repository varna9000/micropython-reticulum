# µReticulum AES
# Uses ucryptolib (hardware AES on ESP32)

from ucryptolib import aes as _aes_impl
_MODE_CBC = 2


class AES_128_CBC:
    @staticmethod
    def encrypt(plaintext, key, iv):
        if len(key) != 16:
            raise ValueError("AES-128 key must be 16 bytes")
        cipher = _aes_impl(key, _MODE_CBC, iv)
        return cipher.encrypt(plaintext)

    @staticmethod
    def decrypt(ciphertext, key, iv):
        if len(key) != 16:
            raise ValueError("AES-128 key must be 16 bytes")
        cipher = _aes_impl(key, _MODE_CBC, iv)
        return cipher.decrypt(ciphertext)


class AES_256_CBC:
    @staticmethod
    def encrypt(plaintext, key, iv):
        if len(key) != 32:
            raise ValueError("AES-256 key must be 32 bytes")
        cipher = _aes_impl(key, _MODE_CBC, iv)
        return cipher.encrypt(plaintext)

    @staticmethod
    def decrypt(ciphertext, key, iv):
        if len(key) != 32:
            raise ValueError("AES-256 key must be 32 bytes")
        cipher = _aes_impl(key, _MODE_CBC, iv)
        return cipher.decrypt(ciphertext)


# Module-level constant for Token.py compatibility
AES = "AES"
