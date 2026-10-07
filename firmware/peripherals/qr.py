# QR code encoder for one job: the device's own "lxma://<addr>:<pubkey>"
# contact URI (168 bytes), which Columba and MeshChatX scan.
#
# That text always fits version 8 (49x49 modules) at error-correction level L
# in byte mode, so that is the only symbol this builds: no version or mode
# selection, and a fixed mask (pattern 0). A scanner reads the mask from the
# format bits, so any mask decodes; choosing the "best" one means scoring all
# eight over 2401 modules, which is seconds of MicroPython for nothing.
#
# encode() returns the modules as a bytearray, one byte per module, row by
# row, 1 = dark. Pure Python, so the same file runs on the stock-MicroPython
# T-Deck Pro and on the host tests.

SIZE = 49            # modules per side, version 8
MAX_BYTES = 192      # byte-mode capacity of 8-L
_VERSION = 8
_DATA_CW = 194       # data codewords, in 2 blocks of 97
_BLOCKS = 2
_EC_CW = 24          # error-correction codewords per block
_ALIGN = (6, 24, 42)


def _gf_tables():
    exp = bytearray(510)
    log = bytearray(256)
    x = 1
    for i in range(255):
        exp[i] = exp[i + 255] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    return exp, log


def _rs_ec(data, exp, log):
    """The _EC_CW Reed-Solomon codewords for one block."""
    # generator polynomial, highest power first, leading 1 dropped
    gen = bytearray(_EC_CW)
    gen[_EC_CW - 1] = 1
    root = 0                              # log of the current root
    for _ in range(_EC_CW):
        for j in range(_EC_CW):
            if gen[j]:
                gen[j] = exp[log[gen[j]] + root]
            if j + 1 < _EC_CW:
                gen[j] ^= gen[j + 1]
        root += 1
    rem = bytearray(_EC_CW)
    for b in data:
        f = b ^ rem[0]
        for j in range(_EC_CW - 1):
            rem[j] = rem[j + 1]
        rem[_EC_CW - 1] = 0
        if f:
            lf = log[f]
            for j in range(_EC_CW):
                if gen[j]:
                    rem[j] ^= exp[log[gen[j]] + lf]
    return rem


def _codewords(payload):
    """Data codewords (mode, length, bytes, padding), then the EC, interleaved."""
    n = len(payload)
    data = bytearray(_DATA_CW)
    # 4-bit mode 0100, 8-bit count, the bytes, 4-bit terminator: every byte
    # straddles two codewords.
    data[0] = 0x40 | (n >> 4)
    prev = n & 0x0F
    for i in range(n):
        b = payload[i]
        data[i + 1] = (prev << 4) | (b >> 4)
        prev = b & 0x0F
    data[n + 1] = prev << 4
    pad = 0xEC
    for i in range(n + 2, _DATA_CW):
        data[i] = pad
        pad ^= 0xEC ^ 0x11
    exp, log = _gf_tables()
    per = _DATA_CW // _BLOCKS
    blocks = [data[i * per:(i + 1) * per] for i in range(_BLOCKS)]
    ecs = [_rs_ec(b, exp, log) for b in blocks]
    out = bytearray()
    for i in range(per):
        for b in blocks:
            out.append(b[i])
    for i in range(_EC_CW):
        for e in ecs:
            out.append(e[i])
    return out


def encode(text):
    """Module grid for text (str or bytes, up to MAX_BYTES): a bytearray of
    SIZE * SIZE, row by row, 1 = dark."""
    payload = text.encode() if isinstance(text, str) else bytes(text)
    if len(payload) > MAX_BYTES:
        raise ValueError("too long for this QR size")
    n = SIZE
    mod = bytearray(n * n)
    fun = bytearray(n * n)                # 1 = function module, not data

    def put(x, y, dark):
        mod[y * n + x] = 1 if dark else 0
        fun[y * n + x] = 1

    # timing patterns
    for i in range(n):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    # finder patterns with their separators
    for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < n and 0 <= y < n:
                    d = max(abs(dx), abs(dy))
                    put(x, y, d != 2 and d != 4)
    # alignment patterns, except where the finders are
    last = len(_ALIGN) - 1
    for i in range(len(_ALIGN)):
        for j in range(len(_ALIGN)):
            if (i == 0 and j == 0) or (i == 0 and j == last) or (i == last and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    put(_ALIGN[i] + dx, _ALIGN[j] + dy, max(abs(dx), abs(dy)) != 1)
    # format bits: level L (01) + mask 0, BCH(15,5), in both copies
    fmt = 1 << 3
    rem = fmt
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = ((fmt << 10) | rem) ^ 0x5412
    for i in range(6):
        put(8, i, (bits >> i) & 1)
    put(8, 7, (bits >> 6) & 1)
    put(8, 8, (bits >> 7) & 1)
    put(7, 8, (bits >> 8) & 1)
    for i in range(9, 15):
        put(14 - i, 8, (bits >> i) & 1)
    for i in range(8):
        put(n - 1 - i, 8, (bits >> i) & 1)
    for i in range(8, 15):
        put(8, n - 15 + i, (bits >> i) & 1)
    put(8, n - 8, 1)                      # the always-dark module
    # version bits (versions 7+): BCH(18,6), two copies
    rem = _VERSION
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    bits = (_VERSION << 12) | rem
    for i in range(18):
        a = n - 11 + i % 3
        b = i // 3
        put(a, b, (bits >> i) & 1)
        put(b, a, (bits >> i) & 1)

    # data: zigzag up and down in two-module columns from the right, skipping
    # the timing column; mask 0 flips modules where (x + y) is even
    cw = _codewords(payload)
    total = len(cw) * 8
    k = 0
    right = n - 1
    while right >= 1:
        if right == 6:
            right = 5
        up = ((right + 1) & 2) == 0
        for v in range(n):
            y = n - 1 - v if up else v
            for x in (right, right - 1):
                p = y * n + x
                if fun[p]:
                    continue
                bit = 0
                if k < total:
                    bit = (cw[k >> 3] >> (7 - (k & 7))) & 1
                    k += 1
                if (x + y) % 2 == 0:
                    bit ^= 1
                mod[p] = bit
        right -= 2
    return mod
