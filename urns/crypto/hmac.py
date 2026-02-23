# µReticulum HMAC
# Based on Reticulum's HMAC.py (from Python 3.10.4)
# Modified to NOT require .copy() on hash objects (MicroPython uhashlib lacks it)

_trans_5C = bytes((x ^ 0x5C) for x in range(256))
_trans_36 = bytes((x ^ 0x36) for x in range(256))

digest_size = None


class HMAC:
    blocksize = 64

    def __init__(self, key, msg=None, digestmod=None):
        if not isinstance(key, (bytes, bytearray)):
            raise TypeError("key: expected bytes or bytearray")

        if digestmod is None:
            # Default to SHA-256 via uhashlib or hashlib
            try:
                import hashlib
                digestmod = hashlib.sha256
            except ImportError:
                from uhashlib import sha256
                digestmod = sha256

        if callable(digestmod):
            self._digest_cons = digestmod
        elif isinstance(digestmod, str):
            import hashlib
            self._digest_cons = lambda d=b'': hashlib.new(digestmod, d)
        else:
            self._digest_cons = lambda d=b'': digestmod.new(d)

        inner = self._digest_cons()
        self.digest_size = inner.digest_size if hasattr(inner, 'digest_size') else 32

        if hasattr(inner, 'block_size'):
            blocksize = inner.block_size
            if blocksize < 16:
                blocksize = self.blocksize
        else:
            blocksize = self.blocksize

        self.block_size = blocksize

        if len(key) > blocksize:
            key = self._digest_cons(key).digest()

        key = key + b'\x00' * (blocksize - len(key))

        # Store padded keys for reconstruction (avoids .copy() requirement)
        self._outer_key_pad = bytes(b ^ 0x5C for b in key)
        self._inner_key_pad = bytes(b ^ 0x36 for b in key)

        # Build inner hash
        self._inner = self._digest_cons()
        self._inner.update(self._inner_key_pad)
        if msg is not None:
            self._inner.update(msg)

    def update(self, msg):
        self._inner.update(msg)

    def digest(self):
        # Recreate outer hash from scratch (no .copy() needed)
        outer = self._digest_cons()
        outer.update(self._outer_key_pad)
        outer.update(self._inner.digest())
        return outer.digest()

    def hexdigest(self):
        import binascii
        return str(binascii.hexlify(self.digest()), 'ascii')


def new(key, msg=None, digestmod=None):
    return HMAC(key, msg, digestmod)


def digest(key, msg, digest_func):
    if digest_func is None:
        try:
            import hashlib
            digest_func = hashlib.sha256
        except ImportError:
            from uhashlib import sha256
            digest_func = sha256

    if callable(digest_func):
        digest_cons = digest_func
    elif isinstance(digest_func, str):
        import hashlib
        digest_cons = lambda d=b'': hashlib.new(digest_func, d)
    else:
        digest_cons = lambda d=b'': digest_func.new(d)

    inner = digest_cons()
    outer = digest_cons()
    blocksize = getattr(inner, 'block_size', 64)
    if len(key) > blocksize:
        key = digest_cons(key).digest()

    key = key + b'\x00' * (blocksize - len(key))
    inner.update(bytes(b ^ 0x36 for b in key))
    outer.update(bytes(b ^ 0x5C for b in key))
    inner.update(msg)
    outer.update(inner.digest())
    return outer.digest()
