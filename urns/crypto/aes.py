# µReticulum AES
# Uses ucryptolib (hardware AES on RP2040) or falls back to PyCryptodome for testing

try:
    from ucryptolib import aes as _aes_impl
    _MODE_CBC = 2
    _BACKEND = "ucryptolib"
except ImportError:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        _BACKEND = "pyca"
    except ImportError:
        try:
            from Crypto.Cipher import AES as _pycrypto_aes
            _BACKEND = "pycryptodome"
        except ImportError:
            raise RuntimeError("No AES implementation available")


class AES_128_CBC:
    @staticmethod
    def encrypt(plaintext, key, iv):
        if len(key) != 16:
            raise ValueError("AES-128 key must be 16 bytes")
        if _BACKEND == "ucryptolib":
            cipher = _aes_impl(key, _MODE_CBC, iv)
            return cipher.encrypt(plaintext)
        elif _BACKEND == "pyca":
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            enc = cipher.encryptor()
            return enc.update(plaintext) + enc.finalize()
        elif _BACKEND == "pycryptodome":
            cipher = _pycrypto_aes.new(key, _pycrypto_aes.MODE_CBC, iv)
            return cipher.encrypt(plaintext)

    @staticmethod
    def decrypt(ciphertext, key, iv):
        if len(key) != 16:
            raise ValueError("AES-128 key must be 16 bytes")
        if _BACKEND == "ucryptolib":
            cipher = _aes_impl(key, _MODE_CBC, iv)
            return cipher.decrypt(ciphertext)
        elif _BACKEND == "pyca":
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            dec = cipher.decryptor()
            return dec.update(ciphertext) + dec.finalize()
        elif _BACKEND == "pycryptodome":
            cipher = _pycrypto_aes.new(key, _pycrypto_aes.MODE_CBC, iv)
            return cipher.decrypt(ciphertext)


class AES_256_CBC:
    @staticmethod
    def encrypt(plaintext, key, iv):
        if len(key) != 32:
            raise ValueError("AES-256 key must be 32 bytes")
        if _BACKEND == "ucryptolib":
            cipher = _aes_impl(key, _MODE_CBC, iv)
            return cipher.encrypt(plaintext)
        elif _BACKEND == "pyca":
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            enc = cipher.encryptor()
            return enc.update(plaintext) + enc.finalize()
        elif _BACKEND == "pycryptodome":
            cipher = _pycrypto_aes.new(key, _pycrypto_aes.MODE_CBC, iv)
            return cipher.encrypt(plaintext)

    @staticmethod
    def decrypt(ciphertext, key, iv):
        if len(key) != 32:
            raise ValueError("AES-256 key must be 32 bytes")
        if _BACKEND == "ucryptolib":
            cipher = _aes_impl(key, _MODE_CBC, iv)
            return cipher.decrypt(ciphertext)
        elif _BACKEND == "pyca":
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            dec = cipher.decryptor()
            return dec.update(ciphertext) + dec.finalize()
        elif _BACKEND == "pycryptodome":
            cipher = _pycrypto_aes.new(key, _pycrypto_aes.MODE_CBC, iv)
            return cipher.decrypt(ciphertext)


# Module-level constant for Token.py compatibility
AES = "AES"
