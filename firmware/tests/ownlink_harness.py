# Two real urns nodes in ONE process for tests that need both ends of a link
# (REAL OutgoingLink on node A, REAL inbound Link on node B, real Identity, real
# Packet/Transport/Resource code). Transport is a class-level singleton, so each Node
# owns its own copy of every Transport container and the harness swaps them in around
# every call into that node ("with node.ctx():"). Frames are moved by hand between the
# nodes' interfaces, which gives tests full control over ordering and loss.
#
# The only stub is ucryptolib (AES is not available under CPython): a keyed XOR stream
# with the same interface, so Token still does IV + HMAC-SHA256 for real. Everything
# the tests assert (Ed25519 proofs, hashes, routing, LXMF) is independent of it.

import asyncio
import hashlib
import importlib
import importlib.machinery
import importlib.util
import os
import sys
import time
import types


def bootstrap():
    mp = types.ModuleType("micropython")
    mp.const = lambda x: x
    mp.native = lambda f: f
    mp.viper = lambda f: f
    sys.modules["micropython"] = mp
    sys.modules.setdefault("uhashlib", hashlib)
    sys.modules.setdefault("uasyncio", asyncio)
    sys.modules.setdefault("machine", types.ModuleType("machine"))
    if not hasattr(asyncio, "sleep_ms"):
        async def _sleep_ms(ms):
            await asyncio.sleep(ms / 1000)
        asyncio.sleep_ms = _sleep_ms

    if not hasattr(time, "ticks_ms"):          # MicroPython's tick API (link.py uses it directly)
        time.ticks_ms = lambda: int(time.monotonic() * 1000)
        time.ticks_diff = lambda a, b: a - b
    uc = types.ModuleType("ucryptolib")

    class _Aes:
        def __init__(self, key, mode, iv):
            self.key, self.iv = bytes(key), bytes(iv)

        def _xor(self, data):
            out = bytearray()
            n = 0
            while len(out) < len(data):
                out += hashlib.sha256(self.key + self.iv + n.to_bytes(4, "big")).digest()
                n += 1
            return bytes(a ^ b for a, b in zip(data, out))

        encrypt = decrypt = _xor

    uc.aes = _Aes
    sys.modules["ucryptolib"] = uc
    fw = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    urns_dir = os.path.join(fw, "urns")
    spec = importlib.machinery.ModuleSpec("urns", loader=None, is_package=True)
    spec.submodule_search_locations = [urns_dir]
    pkg = importlib.util.module_from_spec(spec)
    pkg.__path__ = [urns_dir]
    sys.modules["urns"] = pkg


bootstrap()
const = importlib.import_module("urns.const")
logmod = importlib.import_module("urns.log")
logmod.set_loglevel(logmod.LOG_NONE)
Identity = importlib.import_module("urns.identity").Identity
destination_mod = importlib.import_module("urns.destination")
Destination = destination_mod.Destination
packet_mod = importlib.import_module("urns.packet")
transport_mod = importlib.import_module("urns.transport")
link_mod = importlib.import_module("urns.link")
resource_mod = importlib.import_module("urns.resource")
lxmf = importlib.import_module("urns.lxmf")
umsgpack = importlib.import_module("urns.umsgpack")
Transport = transport_mod.Transport
Packet = packet_mod.Packet

_SWAP_SCALARS = ("identity", "announce_inline", "stamp_path_requests", "_jobs_running")


class Iface:
    def __init__(self, name):
        self.name = name
        self.online = True
        self.OUT = True
        self.IN = True
        self.HW_MTU = 1064
        self.bitrate = 10_000_000
        self.ifac_signing_key = None
        self.ifac_key = None
        self.ifac_size = 0
        self.rssi = None
        self.snr = None
        self.gravity = 0
        self.sent = []

    def process_outgoing(self, data):
        self.sent.append(bytes(data))
        return True

    def __str__(self):
        return self.name


_CONTAINERS = [k for k, v in vars(Transport).items()
               if not k.startswith("__") and isinstance(v, (list, dict, set))]
_PRISTINE = {k: type(getattr(Transport, k))(getattr(Transport, k)) for k in _CONTAINERS}


class Node:
    def __init__(self, name):
        self.name = name
        self.identity = Identity()
        self.iface = Iface(name)
        self.state = {k: type(v)(v) for k, v in _PRISTINE.items()}
        self.scalars = {"identity": self.identity, "announce_inline": True,
                        "stamp_path_requests": False, "_jobs_running": False}
        self.state["interfaces"].append(self.iface)

    def ctx(self):
        return _Ctx(self)

    def take(self):
        out, self.iface.sent = self.iface.sent, []
        return out

    def deliver(self, frame):
        with self.ctx():
            Transport.inbound(frame, self.iface)


class _Ctx:
    def __init__(self, node):
        self.node = node

    def __enter__(self):
        self.saved = ({k: getattr(Transport, k) for k in _CONTAINERS},
                      {k: getattr(Transport, k) for k in _SWAP_SCALARS})
        for k, v in self.node.state.items():
            setattr(Transport, k, v)
        for k, v in self.node.scalars.items():
            setattr(Transport, k, v)

    def __exit__(self, *a):
        for k in _SWAP_SCALARS:
            self.node.scalars[k] = getattr(Transport, k)
        for k, v in self.saved[0].items():
            setattr(Transport, k, v)
        for k, v in self.saved[1].items():
            setattr(Transport, k, v)
        return False


def parse(frame):
    p = Packet(destination=None, data=frame)
    assert p.unpack()
    return p
