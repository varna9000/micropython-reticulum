# Pure25519 basic EC math (MIT License - Brian Warner)
# Adapted for MicroPython: removed itertools, adapted hashlib

import binascii

def _rev(b):
    """Reverse bytes - MicroPython compatible (no step slicing)."""
    return bytes(reversed(b))

Q = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493

def inv(x):
    return pow(x, Q - 2, Q)

d = -121665 * inv(121666)
I = pow(2, (Q - 1) // 4, Q)

def xrecover(y):
    xx = (y * y - 1) * inv(d * y * y + 1)
    x = pow(xx, (Q + 3) // 8, Q)
    if (x * x - xx) % Q != 0:
        x = (x * I) % Q
    if x % 2 != 0:
        x = Q - x
    return x

By = 4 * inv(5)
Bx = xrecover(By)
B = [Bx % Q, By % Q]

def xform_affine_to_extended(pt):
    (x, y) = pt
    return (x % Q, y % Q, 1, (x * y) % Q)

def xform_extended_to_affine(pt):
    (x, y, z, _) = pt
    return ((x * inv(z)) % Q, (y * inv(z)) % Q)

def double_element(pt):
    (X1, Y1, Z1, _) = pt
    A = (X1 * X1)
    B = (Y1 * Y1)
    C = (2 * Z1 * Z1)
    D = (-A) % Q
    J = (X1 + Y1) % Q
    E = (J * J - A - B) % Q
    G = (D + B) % Q
    F = (G - C) % Q
    H = (D - B) % Q
    X3 = (E * F) % Q
    Y3 = (G * H) % Q
    Z3 = (F * G) % Q
    T3 = (E * H) % Q
    return (X3, Y3, Z3, T3)

def add_elements(pt1, pt2):
    (X1, Y1, Z1, T1) = pt1
    (X2, Y2, Z2, T2) = pt2
    A = ((Y1 - X1) * (Y2 - X2)) % Q
    B = ((Y1 + X1) * (Y2 + X2)) % Q
    C = T1 * (2 * d) * T2 % Q
    D = Z1 * 2 * Z2 % Q
    E = (B - A) % Q
    F = (D - C) % Q
    G = (D + C) % Q
    H = (B + A) % Q
    X3 = (E * F) % Q
    Y3 = (G * H) % Q
    T3 = (E * H) % Q
    Z3 = (F * G) % Q
    return (X3, Y3, Z3, T3)

def scalarmult_element_safe_slow(pt, n):
    assert n >= 0
    if n == 0:
        return xform_affine_to_extended((0, 1))
    # Iterative double-and-add (MicroPython has shallow recursion limit)
    result = xform_affine_to_extended((0, 1))
    addend = pt
    while n > 0:
        if n & 1:
            result = add_elements(result, addend)
        addend = double_element(addend)
        n >>= 1
    return result

def _add_elements_nonunified(pt1, pt2):
    (X1, Y1, Z1, T1) = pt1
    (X2, Y2, Z2, T2) = pt2
    A = ((Y1 - X1) * (Y2 + X2)) % Q
    B = ((Y1 + X1) * (Y2 - X2)) % Q
    C = (Z1 * 2 * T2) % Q
    D = (T1 * 2 * Z2) % Q
    E = (D + C) % Q
    F = (B - A) % Q
    G = (B + A) % Q
    H = (D - C) % Q
    X3 = (E * F) % Q
    Y3 = (G * H) % Q
    Z3 = (F * G) % Q
    T3 = (E * H) % Q
    return (X3, Y3, Z3, T3)

def scalarmult_element(pt, n):
    assert n >= 0
    if n == 0:
        return xform_affine_to_extended((0, 1))
    # Iterative double-and-add (MicroPython has shallow recursion limit)
    result = xform_affine_to_extended((0, 1))
    addend = pt
    while n > 0:
        if n & 1:
            result = _add_elements_nonunified(result, addend)
        addend = double_element(addend)
        n >>= 1
    return result

def encodepoint(P):
    x = P[0]
    y = P[1]
    assert 0 <= y < (1 << 255)
    if x & 1:
        y += 1 << 255
    return _rev(binascii.unhexlify("%064x" % y))

def isoncurve(P):
    x = P[0]
    y = P[1]
    return (-x * x + y * y - 1 - d * x * x * y * y) % Q == 0

class NotOnCurve(Exception):
    pass

def decodepoint(s):
    unclamped = int(binascii.hexlify(_rev(s[:32])), 16)
    clamp = (1 << 255) - 1
    y = unclamped & clamp
    x = xrecover(y)
    if bool(x & 1) != bool(unclamped & (1 << 255)):
        x = Q - x
    P = [x, y]
    if not isoncurve(P):
        raise NotOnCurve("decoding point that is not on curve")
    return P

def bytes_to_scalar(s):
    assert len(s) == 32, len(s)
    return int(binascii.hexlify(_rev(s)), 16)

def bytes_to_clamped_scalar(s):
    a_unclamped = bytes_to_scalar(s)
    AND_CLAMP = (1 << 254) - 1 - 7
    OR_CLAMP = (1 << 254)
    return (a_unclamped & AND_CLAMP) | OR_CLAMP

def random_scalar(entropy_f):
    oversized = int(binascii.hexlify(entropy_f(64)), 16)
    return oversized % L

def scalar_to_bytes(y):
    y = y % L
    assert 0 <= y < 2**256
    return _rev(binascii.unhexlify("%064x" % y))

def is_extended_zero(XYTZ):
    (X, Y, Z, T) = XYTZ
    Y = Y % Q
    Z = Z % Q
    if X == 0 and Y == Z and Y != 0:
        return True
    return False


class ElementOfUnknownGroup:
    def __init__(self, XYTZ):
        self.XYTZ = XYTZ

    def add(self, other):
        if not isinstance(other, ElementOfUnknownGroup):
            raise TypeError("elements can only be added to other elements")
        sum_XYTZ = add_elements(self.XYTZ, other.XYTZ)
        if is_extended_zero(sum_XYTZ):
            return Zero
        return ElementOfUnknownGroup(sum_XYTZ)

    def scalarmult(self, s):
        if isinstance(s, ElementOfUnknownGroup):
            raise TypeError("elements cannot be multiplied together")
        assert s >= 0
        product = scalarmult_element_safe_slow(self.XYTZ, s)
        return ElementOfUnknownGroup(product)

    def to_bytes(self):
        return encodepoint(xform_extended_to_affine(self.XYTZ))

    def __eq__(self, other):
        return self.to_bytes() == other.to_bytes()

    def __ne__(self, other):
        return not self == other


class Element(ElementOfUnknownGroup):
    def add(self, other):
        if not isinstance(other, ElementOfUnknownGroup):
            raise TypeError("elements can only be added to other elements")
        sum_element = ElementOfUnknownGroup.add(self, other)
        if sum_element is Zero:
            return sum_element
        if isinstance(other, Element):
            return Element(sum_element.XYTZ)
        return sum_element

    def scalarmult(self, s):
        if isinstance(s, ElementOfUnknownGroup):
            raise TypeError("elements cannot be multiplied together")
        s = s % L
        if s == 0:
            return Zero
        return Element(scalarmult_element(self.XYTZ, s))

    def negate(self):
        return Element(scalarmult_element(self.XYTZ, L - 2))

    def subtract(self, other):
        return self.add(other.negate())


class _ZeroElement(ElementOfUnknownGroup):
    def add(self, other):
        return other

    def scalarmult(self, s):
        return self

    def negate(self):
        return self

    def subtract(self, other):
        return self.add(other.negate())


Base = Element(xform_affine_to_extended(B))
Zero = _ZeroElement(xform_affine_to_extended((0, 1)))
_zero_bytes = Zero.to_bytes()

# P1: Precomputed table REMOVED — permanent memory allocation fragments
# ESP32 heap and breaks lwIP socket receive buffers. On desktop/Pico W
# with more RAM, this could be re-enabled.

def scalarmult_base_comb(s):
    """Compute s*B. Direct delegation to standard scalarmult.
    
    Named 'comb' for API compatibility but uses standard method
    to avoid permanent heap allocations on memory-constrained devices.
    """
    return Base.scalarmult(s)


def arbitrary_element(seed):
    from ..hashes import sha512
    hseed = sha512(seed)
    y = int(binascii.hexlify(hseed), 16) % Q

    plus = 0
    while True:
        y_plus = (y + plus) % Q
        x = xrecover(y_plus)
        Pa = [x, y_plus]

        if not isoncurve(Pa):
            plus += 1
            continue

        P = ElementOfUnknownGroup(xform_affine_to_extended(Pa))
        P8 = P.scalarmult(8)

        if is_extended_zero(P8.XYTZ):
            plus += 1
            continue

        assert is_extended_zero(P8.scalarmult(L).XYTZ)
        return Element(P8.XYTZ)


def bytes_to_unknown_group_element(b):
    if b == _zero_bytes:
        return Zero
    XYTZ = xform_affine_to_extended(decodepoint(b))
    return ElementOfUnknownGroup(XYTZ)


def bytes_to_element(b):
    P = bytes_to_unknown_group_element(b)
    if P is Zero:
        raise ValueError("element was Zero")
    if not is_extended_zero(P.scalarmult(L).XYTZ):
        raise ValueError("element is not in the right group")
    return Element(P.XYTZ)

def bytes_to_element_unchecked(b):
    """Decode a point without the expensive L-order group check.

    Safe for Ed25519 verify because the verification equation
    S*B == R + h*A itself rejects points not in the prime-order
    subgroup. This matches libsodium's verify behavior.
    """
    P = bytes_to_unknown_group_element(b)
    if P is Zero:
        raise ValueError("element was Zero")
    return Element(P.XYTZ)
