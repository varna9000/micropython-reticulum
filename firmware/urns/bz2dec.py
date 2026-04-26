# µReticulum bz2 decompressor
# Ported from pyflate (pfalcon) — BSD/GPLv2
#
# Automatically uses native C module (bz2_fast) if available
# for the current architecture, otherwise falls back to pure Python.

import io
import sys

_native = None

def _try_native():
    global _native
    mod = None
    try:
        if sys.platform == "esp32":
            import bz2_fast_xtensawin
            mod = bz2_fast_xtensawin
        elif sys.platform == "rp2":
            import bz2_fast_armv6m
            mod = bz2_fast_armv6m
        else:
            import bz2_fast
            mod = bz2_fast
    except ImportError:
        pass
    if mod is None:
        try:
            import bz2_fast
            mod = bz2_fast
        except ImportError:
            pass
    _native = mod

_try_native()

if _native:
    from .log import log, LOG_VERBOSE
    log("bz2: native C module loaded", LOG_VERBOSE)


class _Bitfield:
    __slots__ = ('f', 'bits', 'bitfield')

    def __init__(self, f):
        self.f = f
        self.bits = 0
        self.bitfield = 0

    def readbits(self, n):
        while self.bits < n:
            c = self.f.read(1)
            if not c:
                raise ValueError("bz2: unexpected end of data")
            self.bitfield = (self.bitfield << 8) | c[0]
            self.bits += 8
        self.bits -= n
        r = (self.bitfield >> self.bits) & ((1 << n) - 1)
        self.bitfield &= (1 << self.bits) - 1
        return r

    def snoopbits(self, n):
        while self.bits < n:
            c = self.f.read(1)
            if not c:
                raise ValueError("bz2: unexpected end of data")
            self.bitfield = (self.bitfield << 8) | c[0]
            self.bits += 8
        return (self.bitfield >> (self.bits - n)) & ((1 << n) - 1)

    def align(self):
        skip = self.bits & 7
        if skip:
            self.readbits(skip)


def _reverse_bits(v, n):
    a = 1
    b = 1 << (n - 1)
    z = 0
    for i in range(n - 1, -1, -2):
        z |= (v >> i) & a
        z |= (v << i) & b
        a <<= 1
        b >>= 1
    return z


class _HL:
    __slots__ = ('code', 'bits', 'symbol')

    def __init__(self, code, bits):
        self.code = code
        self.bits = bits
        self.symbol = 0

    def __lt__(self, other):
        if self.bits == other.bits:
            return self.code < other.code
        return self.bits < other.bits


class _HuffTable:
    def __init__(self, lengths):
        t = []
        for i in range(len(lengths)):
            if lengths[i]:
                t.append(_HL(i, lengths[i]))
        t.sort()
        self.table = t

    def populate(self):
        bits = -1
        symbol = -1
        for x in self.table:
            symbol += 1
            if x.bits != bits:
                symbol <<= (x.bits - bits)
                bits = x.bits
            x.symbol = symbol

        # Build fast lookup: {bit_length: {canonical_symbol: code}}
        self._by_bits = {}
        for x in self.table:
            d = self._by_bits.get(x.bits)
            if d is None:
                d = {}
                self._by_bits[x.bits] = d
            d[x.symbol] = x.code
        self._bit_lengths = sorted(self._by_bits.keys())

    def find(self, field):
        for b in self._bit_lengths:
            bits_val = field.snoopbits(b)
            code = self._by_bits[b].get(bits_val)
            if code is not None:
                field.readbits(b)
                return code
        raise ValueError("bz2: huffman symbol not found")


def _mtf(l, c):
    l.insert(0, l.pop(c))


def _bwt_reverse(data, end):
    n = len(data)
    if n == 0:
        return b""

    counts = [0] * 256
    for b in data:
        counts[b] += 1

    base = [0] * 256
    total = 0
    for i in range(256):
        base[i] = total
        total += counts[i]
    del counts

    pointers = [0] * n
    for i in range(n):
        s = data[i]
        pointers[base[s]] = i
        base[s] += 1
    del base

    out = bytearray(n)
    for i in range(n):
        end = pointers[end]
        out[i] = data[end]
    del pointers
    return out


def decompress(data):
    """Decompress bz2-compressed bytes. Returns bytes."""
    if _native:
        return _native.decompress(data)
    return _decompress_python(data)


def compress(data):
    """Compress bytes with bz2. Requires native C module. Returns bytes or None."""
    if _native and hasattr(_native, 'compress'):
        return _native.compress(data)
    return None


def _decompress_python(data):
    """Pure Python fallback decompressor."""
    f = io.BytesIO(data)

    magic = f.read(2)
    if magic != b'BZ':
        raise ValueError("bz2: bad magic")

    b = _Bitfield(f)

    method = b.readbits(8)
    if method != 0x68:  # 'h'
        raise ValueError("bz2: unknown method")

    blocksize = b.readbits(8)
    if not (0x31 <= blocksize <= 0x39):  # '1'-'9'
        raise ValueError("bz2: unknown blocksize")

    output = bytearray()

    while True:
        blocktype = b.readbits(48)
        _crc = b.readbits(32)

        if blocktype == 0x314159265359:  # data block
            if b.readbits(1):
                raise ValueError("bz2: randomised not supported")

            pointer = b.readbits(24)

            # Read used character map
            used_map = b.readbits(16)
            used = []
            mask = 1 << 15
            while mask > 0:
                if used_map & mask:
                    bitmap = b.readbits(16)
                    bit = 1 << 15
                    while bit > 0:
                        used.append(bool(bitmap & bit))
                        bit >>= 1
                else:
                    for _ in range(16):
                        used.append(False)
                mask >>= 1

            # Read selectors
            n_groups = b.readbits(3)
            n_selectors = b.readbits(15)

            mtf_groups = list(range(n_groups))
            selectors = []
            for _ in range(n_selectors):
                c = 0
                while b.readbits(1):
                    c += 1
                _mtf(mtf_groups, c)
                selectors.append(mtf_groups[0])

            # Read Huffman tables
            n_symbols = sum(used) + 2
            tables = []
            for _ in range(n_groups):
                length = b.readbits(5)
                lengths = []
                for _s in range(n_symbols):
                    while b.readbits(1):
                        length -= (b.readbits(1) * 2) - 1
                    lengths.append(length)
                t = _HuffTable(lengths)
                t.populate()
                tables.append(t)

            # Decode block
            favourites = []
            for i in range(256):
                if used[i]:
                    favourites.append(i)

            sel_ptr = 0
            decoded = 0
            repeat = 0
            repeat_power = 0
            buf = bytearray()
            t = None

            while True:
                decoded -= 1
                if decoded <= 0:
                    decoded = 50
                    if sel_ptr < len(selectors):
                        t = tables[selectors[sel_ptr]]
                        sel_ptr += 1

                r = t.find(b)

                if r <= 1:
                    if repeat == 0:
                        repeat_power = 1
                    repeat += repeat_power << r
                    repeat_power <<= 1
                    continue
                elif repeat > 0:
                    buf.extend(bytes([favourites[0]]) * repeat)
                    repeat = 0

                if r == n_symbols - 1:
                    break
                else:
                    _mtf(favourites, r - 1)
                    buf.append(favourites[0])

            # Inverse BWT
            import gc; gc.collect()
            decoded_block = _bwt_reverse(buf, pointer)
            del buf
            gc.collect()

            # Inverse RLE
            i = 0
            n = len(decoded_block)
            while i < n:
                if i < n - 4 and \
                   decoded_block[i] == decoded_block[i + 1] == \
                   decoded_block[i + 2] == decoded_block[i + 3]:
                    v = decoded_block[i]
                    count = decoded_block[i + 4] + 4
                    output.extend(bytes([v]) * count)
                    i += 5
                else:
                    output.append(decoded_block[i])
                    i += 1

            del decoded_block
            import gc; gc.collect()

        elif blocktype == 0x177245385090:  # end of stream
            b.align()
            break
        else:
            raise ValueError("bz2: unknown blocktype")

    return bytes(output)
