#!/usr/bin/env python3
"""Generate a Reticulum 1.6 announce stamp off-device (standard library only).

A 1.6 hub that enforces stamps wants sha256(dest_hash + public_key + name_hash
+ stamp) <= target(25.0 by default). A microcontroller needs minutes to hours
for that; this does it in seconds on a PC and writes the .dstamp file the
firmware (and the reference RNS) load from <storage>/stamps/.

usage:
  gen_announce_stamp.py DEST_HASH PUBLIC_KEY NAME_HASH [options]   (3 hex strings:
                                                                    16, 64, 10 bytes)
  gen_announce_stamp.py --material HEX [options]   (the one hex string the device logs:
                                                    "stamp material <hex>", 90 bytes)
options:
  --value V      stamp value to reach (default 25.0)
  --out DIR      directory for the .dstamp file (default .)
  --storage DIR  device storage dir used in the printed upload command (default /rns)
  --procs N      worker processes (default: all CPUs)
"""
import argparse
import hashlib
import multiprocessing
import os
import struct
import sys
import time

MANTISSA_BITS = 33
COST_FP = 256
STAMP_LEN = 14
CAP = 100


def _iroot(n, k):
    """floor(n ** (1/k)) by integer Newton iteration."""
    x = 1 << ((n.bit_length() + k - 1) // k)
    while True:
        y = ((k - 1) * x + n // x ** (k - 1)) // k
        if y >= x:
            return x
        x = y


def _tab(f):
    # TAB[f] = floor(2^33 * 2^(f/256)), exactly (reference RNS/Stamps.py TAB)
    return _iroot(1 << (MANTISSA_BITS * 256 + f), 256)


_TAB = None


def tab():
    global _TAB
    if _TAB is None:
        _TAB = [_tab(f) for f in range(256)]
    return _TAB


def cost_to_fp(cost):
    return max(0, min(int(float(cost) * COST_FP + 0.5), CAP * COST_FP))


def target_from_fp(fp):
    i, f = divmod(256 * COST_FP - fp, COST_FP)
    return (tab()[f] << i) >> MANTISSA_BITS


def stamp_value(material, stamp):
    hn = int.from_bytes(hashlib.sha256(material + stamp).digest(), "big")
    if hn == 0:
        return 256.0
    nb = MANTISSA_BITS + 1
    bl = hn.bit_length()
    lz = 256 - bl
    mn = (hn >> (bl - nb)) if bl > nb else (hn << (nb - bl))
    import bisect
    return (((lz + 1) << 8) - bisect.bisect_right(tab(), mn)) / COST_FP


def _worker(args):
    material, target, seed, q, stop = args
    base = hashlib.sha256(material)
    n = 0
    tgt = target.to_bytes(32, "big")
    while not stop.is_set():
        for _ in range(20000):
            n += 1
            stamp = seed + n.to_bytes(6, "little")          # 8 + 6 = 14 bytes
            h = base.copy()
            h.update(stamp)
            if h.digest() <= tgt:
                q.put((stamp, n % 20000 or 20000))
                return
        q.put((None, 20000))                                 # progress tick
    return


def search(material, value, procs):
    target = target_from_fp(cost_to_fp(value))
    mgr = multiprocessing.Manager()
    q = mgr.Queue()
    stop = mgr.Event()
    ps = [multiprocessing.Process(target=_worker, args=((material, target, os.urandom(8), q, stop),))
          for _ in range(procs)]
    t0 = time.time()
    for p in ps:
        p.start()
    done = 0
    while True:
        r, n = q.get()
        done += n
        if r is None:
            continue
        stop.set()
        break
    for p in ps:
        p.join(2)
        if p.is_alive():
            p.terminate()
    dt = time.time() - t0
    return r, dt, done


def pack_file(stamp, value):
    """msgpack {"v": 1, "stamp": bin, "value": float64, "generated": float64}."""
    def s(x):
        return bytes((0xA0 | len(x),)) + x.encode()
    return (b"\x84" + s("v") + b"\x01" + s("stamp") + b"\xc4" + bytes((len(stamp),)) + stamp
            + s("value") + b"\xcb" + struct.pack(">d", value)
            + s("generated") + b"\xcb" + struct.pack(">d", time.time()))


def main():
    ap = argparse.ArgumentParser(usage=__doc__, add_help=False)
    ap.add_argument("hex", nargs="*")
    ap.add_argument("--material")
    ap.add_argument("--value", type=float, default=25.0)
    ap.add_argument("--out", default=".")
    ap.add_argument("--storage", default="/rns")
    ap.add_argument("--procs", type=int, default=os.cpu_count() or 1)
    ap.add_argument("-h", "--help", action="store_true")
    a = ap.parse_args()
    if a.help:
        print(__doc__)
        return 0
    try:
        if a.material:
            material = bytes.fromhex("".join(a.material.split()))
        else:
            if len(a.hex) != 3:
                raise ValueError("need DEST_HASH PUBLIC_KEY NAME_HASH or --material")
            material = b"".join(bytes.fromhex(x) for x in a.hex)
            if [len(bytes.fromhex(x)) for x in a.hex] != [16, 64, 10]:
                raise ValueError("expected 16, 64 and 10 bytes")
        if len(material) != 90:
            raise ValueError("material must be 90 bytes (16 + 64 + 10), got %d" % len(material))
    except ValueError as e:
        print("error: %s\n" % e, file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2
    dest = material[:16].hex()
    stamp, dt, tries = search(material, a.value, max(1, a.procs))
    value = stamp_value(material, stamp)
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, dest + ".dstamp")
    with open(path, "wb") as f:
        f.write(pack_file(stamp, value))
    print("stamp %s value %.4f (>= %g) found in %.1f s (~%d hashes/s on %d procs)"
          % (stamp.hex(), value, a.value, dt, tries / dt if dt else 0, a.procs))
    print("wrote", path)
    print("upload:")
    print("  mpremote resume mkdir :%s/stamps   # once; ignore 'file exists'" % a.storage)
    print("  mpremote resume cp %s :%s/stamps/%s.dstamp" % (path, a.storage, dest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
