# Pure25519 _ed25519 bridge (MIT License - Brian Warner)
# Adapted for µReticulum

from . import eddsa

class BadSignatureError(Exception):
    pass

SECRETKEYBYTES = 64
PUBLICKEYBYTES = 32
SIGNATUREKEYBYTES = 64

def publickey(seed32):
    assert len(seed32) == 32
    vk32 = eddsa.publickey(seed32)
    return vk32, seed32 + vk32

def sign(msg, skvk):
    assert len(skvk) == 64
    sk = skvk[:32]
    vk = skvk[32:]
    sig = eddsa.signature(msg, sk, vk)
    return sig + msg

def open(sigmsg, vk):
    assert len(vk) == 32
    sig = sigmsg[:64]
    msg = sigmsg[64:]
    try:
        valid = eddsa.checkvalid(sig, msg, vk)
    except ValueError as e:
        raise BadSignatureError(e)
    except Exception as e:
        if str(e) == "decoding point that is not on curve":
            raise BadSignatureError(e)
        raise
    if not valid:
        raise BadSignatureError()
    return msg
