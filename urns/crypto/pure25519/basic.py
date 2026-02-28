# Pure25519 basic EC math (MIT License - Brian Warner)
# Adapted for MicroPython: removed itertools, adapted hashlib

Q = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493

def inv(x):
    return pow(x, Q - 2, Q)

# Pre-computed constants (avoid expensive pow() at import time)
d = 37095705934669439343138083508754565189542113879843219016388785533085940283555
I = 19681161376707505956807079304988542015446066515923890162744021073123829784752

def xrecover(y):
    xx = (y * y - 1) * inv(d * y * y + 1)
    x = pow(xx, (Q + 3) // 8, Q)
    if (x * x - xx) % Q != 0:
        x = (x * I) % Q
    if x % 2 != 0:
        x = Q - x
    return x

# Pre-computed base point
By = 46316835694926478169428394003475163141307993866256225615783033603165251855960
Bx = 15112221349535400772501151409588531511454012693041857206046113283949847762202
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

def _double_into(pt, out):
    X1 = pt[0]; Y1 = pt[1]; Z1 = pt[2]
    A = (X1 * X1)
    B = (Y1 * Y1)
    C = (2 * Z1 * Z1)
    D = (-A) % Q
    J = (X1 + Y1) % Q
    E = (J * J - A - B) % Q
    G = (D + B) % Q
    F = (G - C) % Q
    H = (D - B) % Q
    out[0] = (E * F) % Q
    out[1] = (G * H) % Q
    out[2] = (F * G) % Q
    out[3] = (E * H) % Q

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

def _add_into(pt1, pt2, out):
    X1 = pt1[0]; Y1 = pt1[1]; Z1 = pt1[2]; T1 = pt1[3]
    X2 = pt2[0]; Y2 = pt2[1]; Z2 = pt2[2]; T2 = pt2[3]
    A = ((Y1 - X1) * (Y2 + X2)) % Q
    B = ((Y1 + X1) * (Y2 - X2)) % Q
    C = (Z1 * 2 * T2) % Q
    D = (T1 * 2 * Z2) % Q
    E = (D + C) % Q
    F = (B - A) % Q
    G = (B + A) % Q
    H = (D - C) % Q
    out[0] = (E * F) % Q
    out[1] = (G * H) % Q
    out[2] = (F * G) % Q
    out[3] = (E * H) % Q

def scalarmult_element(pt, n):
    assert n >= 0
    if n == 0:
        return xform_affine_to_extended((0, 1))
    result = [0, 1, 1, 0]                        # extended identity
    addend = [pt[0], pt[1], pt[2], pt[3]]
    tmp = [0, 0, 0, 0]
    try:
        import gc
        _gc = gc.collect
    except:
        _gc = None
    _i = 0
    while n > 0:
        if n & 1:
            _add_into(result, addend, tmp)
            result, tmp = tmp, result              # swap refs, no alloc
        _double_into(addend, tmp)
        addend, tmp = tmp, addend                  # swap refs, no alloc
        n >>= 1
        _i += 1
        if _gc and _i & 1 == 0:
            _gc()
    return (result[0], result[1], result[2], result[3])

def encodepoint(P):
    x = P[0]
    y = P[1]
    assert 0 <= y < (1 << 255)
    if x & 1:
        y += 1 << 255
    return y.to_bytes(32, "little")

def isoncurve(P):
    x = P[0]
    y = P[1]
    return (-x * x + y * y - 1 - d * x * x * y * y) % Q == 0

class NotOnCurve(Exception):
    pass

def decodepoint(s):
    unclamped = int.from_bytes(s[:32], "little")
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
    return int.from_bytes(s, "little")

def bytes_to_clamped_scalar(s):
    a_unclamped = bytes_to_scalar(s)
    AND_CLAMP = (1 << 254) - 1 - 7
    OR_CLAMP = (1 << 254)
    return (a_unclamped & AND_CLAMP) | OR_CLAMP

def random_scalar(entropy_f):
    import binascii
    oversized = int(binascii.hexlify(entropy_f(64)), 16)
    return oversized % L

def scalar_to_bytes(y):
    y = y % L
    assert 0 <= y < 2**256
    return y.to_bytes(32, "little")

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
        # Compare in projective coordinates to avoid 2 expensive inv() calls
        X1, Y1, Z1, _ = self.XYTZ
        X2, Y2, Z2, _ = other.XYTZ
        return ((X1 * Z2 - X2 * Z1) % Q == 0 and
                (Y1 * Z2 - Y2 * Z1) % Q == 0)

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
# Pre-computed: Zero.to_bytes() == encodepoint(xform_extended_to_affine((0,1,1,0)))
# which is (0,1) -> y=1, x=0 (even) -> 1.to_bytes(32, "little")
_zero_bytes = b'\x01' + b'\x00' * 31

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
    y = int.from_bytes(hseed, "little") % Q

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
