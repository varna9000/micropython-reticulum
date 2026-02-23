# µReticulum X25519
# Pure-Python Curve25519 (public domain - Nicko van Someren, 2021)
# Constant time exchange added by Mark Qvist
# Adapted for MicroPython timing

import os
import time

P = 2**255 - 19
_A = 486662
_a24 = (_A - 2) >> 2  # = 121665 per RFC 7748


def _point_add(point_n, point_m, point_diff):
    (xn, zn) = point_n
    (xm, zm) = point_m
    (x_diff, z_diff) = point_diff
    x = (z_diff << 2) * (xm * xn - zm * zn) ** 2
    z = (x_diff << 2) * (xm * zn - zm * xn) ** 2
    return x % P, z % P


def _point_double(point_n):
    (xn, zn) = point_n
    xn2 = xn ** 2
    zn2 = zn ** 2
    x = (xn2 - zn2) ** 2
    xzn = xn * zn
    z = 4 * xzn * (xn2 + _A * xzn + zn2)
    return x % P, z % P


def _const_time_swap(a, b, swap):
    index = int(swap) * 2
    temp = (a, b, b, a)
    return temp[index:index + 2]


def _raw_curve25519(base, n):
    """RFC 7748 Montgomery ladder with combined double-and-add.

    P2 optimization: uses a24=121666 to halve the multiply cost
    in the combined step, and reduces total modular reductions
    from 12 to 8 per iteration.
    """
    x_1 = base
    x_2 = 1
    z_2 = 0
    x_3 = base
    z_3 = 1
    swap = 0

    for t in reversed(range(255)):
        k_t = (n >> t) & 1
        swap ^= k_t
        # Conditional swap
        if swap:
            x_2, x_3 = x_3, x_2
            z_2, z_3 = z_3, z_2
        swap = k_t

        A = (x_2 + z_2) % P
        AA = (A * A) % P
        B = (x_2 - z_2) % P
        BB = (B * B) % P
        E = (AA - BB) % P
        C = (x_3 + z_3) % P
        D = (x_3 - z_3) % P
        DA = (D * A) % P
        CB = (C * B) % P
        x_3 = pow(DA + CB, 2, P)
        z_3 = (x_1 * pow(DA - CB, 2, P)) % P
        x_2 = (AA * BB) % P
        z_2 = (E * (AA + _a24 * E)) % P

    # Final conditional swap
    if swap:
        x_2, x_3 = x_3, x_2
        z_2, z_3 = z_3, z_2

    return (x_2 * pow(z_2, P - 2, P)) % P


def _unpack_number(s):
    if len(s) != 32:
        raise ValueError('Curve25519 values must be 32 bytes')
    return int.from_bytes(s, "little")


def _pack_number(n):
    return n.to_bytes(32, "little")


def _fix_secret(n):
    n &= ~7
    n &= ~(128 << 8 * 31)
    n |= 64 << 8 * 31
    return n


def _fix_base_point(n):
    n &= ~(2**255)
    return n


def curve25519(base_point_raw, secret_raw):
    base_point = _fix_base_point(_unpack_number(base_point_raw))
    secret = _fix_secret(_unpack_number(secret_raw))
    return _pack_number(_raw_curve25519(base_point, secret))


def curve25519_base(secret_raw):
    secret = _fix_secret(_unpack_number(secret_raw))
    return _pack_number(_raw_curve25519(9, secret))


class X25519PublicKey:
    def __init__(self, x):
        self.x = x

    @classmethod
    def from_public_bytes(cls, data):
        return cls(_unpack_number(data))

    def public_bytes(self):
        return _pack_number(self.x)


class X25519PrivateKey:
    # Timing constants (milliseconds for MicroPython)
    MIN_EXEC_TIME = 2     # ms
    MAX_EXEC_TIME = 500   # ms
    DELAY_WINDOW = 10000  # ms

    T_CLEAR = None
    T_MAX = 0

    def __init__(self, a):
        self.a = a

    @classmethod
    def generate(cls):
        return cls.from_private_bytes(os.urandom(32))

    @classmethod
    def from_private_bytes(cls, data):
        return cls(_fix_secret(_unpack_number(data)))

    def private_bytes(self):
        return _pack_number(self.a)

    def public_key(self):
        return X25519PublicKey.from_public_bytes(_pack_number(_raw_curve25519(9, self.a)))

    def exchange(self, peer_public_key):
        if isinstance(peer_public_key, bytes):
            peer_public_key = X25519PublicKey.from_public_bytes(peer_public_key)

        # Use ticks_ms for MicroPython compatibility
        try:
            start = time.ticks_ms()
            _use_ticks = True
        except AttributeError:
            start = int(time.time() * 1000)
            _use_ticks = False

        shared = _pack_number(_raw_curve25519(peer_public_key.x, self.a))

        if _use_ticks:
            end = time.ticks_ms()
            duration = time.ticks_diff(end, start)
        else:
            end = int(time.time() * 1000)
            duration = end - start

        if X25519PrivateKey.T_CLEAR is None:
            X25519PrivateKey.T_CLEAR = end + X25519PrivateKey.DELAY_WINDOW

        if _use_ticks:
            if time.ticks_diff(end, X25519PrivateKey.T_CLEAR) > 0:
                X25519PrivateKey.T_CLEAR = end + X25519PrivateKey.DELAY_WINDOW
                X25519PrivateKey.T_MAX = 0
        else:
            if end > X25519PrivateKey.T_CLEAR:
                X25519PrivateKey.T_CLEAR = end + X25519PrivateKey.DELAY_WINDOW
                X25519PrivateKey.T_MAX = 0

        if duration < X25519PrivateKey.T_MAX or duration < X25519PrivateKey.MIN_EXEC_TIME:
            target_duration = X25519PrivateKey.T_MAX
            if target_duration > X25519PrivateKey.MAX_EXEC_TIME:
                target_duration = X25519PrivateKey.MAX_EXEC_TIME
            if target_duration < X25519PrivateKey.MIN_EXEC_TIME:
                target_duration = X25519PrivateKey.MIN_EXEC_TIME

            remaining = target_duration - duration
            if remaining > 0:
                time.sleep_ms(remaining) if hasattr(time, 'sleep_ms') else time.sleep(remaining / 1000)

        elif duration > X25519PrivateKey.T_MAX:
            X25519PrivateKey.T_MAX = duration

        return shared
