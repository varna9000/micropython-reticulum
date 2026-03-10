# µReticulum HKDF (RFC 5869)

from .hmac import new as hmac_new


def hkdf(length=None, derive_from=None, salt=None, context=None):
    hash_len = 32

    def hmac_sha256(key, data):
        return hmac_new(key, data).digest()

    if length is None or length < 1:
        raise ValueError("Invalid output key length")

    if derive_from is None or derive_from == b"":
        raise ValueError("Cannot derive key from empty input material")

    if salt is None or len(salt) == 0:
        salt = bytes(hash_len)

    if context is None:
        context = b""

    pseudorandom_key = hmac_sha256(salt, derive_from)

    block = b""
    derived = b""

    # ceil(length / hash_len) without importing math
    n_blocks = (length + hash_len - 1) // hash_len
    for i in range(n_blocks):
        block = hmac_sha256(pseudorandom_key, block + context + bytes([(i + 1) % 256]))
        derived += block

    return derived[:length]
