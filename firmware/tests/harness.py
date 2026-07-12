# Host-side test harness for µReticulum Transport routing logic.
#
# Runs the REAL urns/transport.py + urns/packet.py + urns/const.py under plain
# CPython (or the micropython unix port) by:
#   1. shimming MicroPython-only modules (micropython, uhashlib, uasyncio, ...)
#   2. registering a synthetic `urns` package whose __path__ points at the real
#      firmware/urns dir — so submodules import WITHOUT running urns/__init__.py
#      (which would pull in lxmf/reticulum/crypto).
#   3. injecting lightweight FAKE `urns.identity` and `urns.destination` modules
#      so routing logic runs crypto-free (hashes use real SHA-256 via hashlib).
#
# Routing decisions (which interface a packet is forwarded on, the rewritten
# bytes, table state) need no real crypto, so this is fast and deterministic.
#
# Usage:  from harness import const, packet, transport, MockInterface, Identity
# Run:    python3 firmware/tests/test_transport.py

import sys
import os
import types
import hashlib
import asyncio


# --------------------------------------------------------------------------
# Fake Identity — crypto-free stand-in. Hashes are real SHA-256 so packet
# dedup behaves correctly; signature validation is stubbed (toggle-able).
# --------------------------------------------------------------------------
class Identity:
    HASHLENGTH = 256
    SIGLENGTH = 512
    TRUNCATED_HASHLENGTH = 128
    KEYSIZE = 512
    NAME_HASH_LENGTH = 80

    validate_result = True        # tests flip this to simulate bad announces
    known = {}                    # dest_hash -> app_data (for recall)
    app_data = {}                 # dest_hash -> announce app_data (recall_app_data)

    @staticmethod
    def full_hash(data):
        return hashlib.sha256(bytes(data)).digest()

    @staticmethod
    def truncated_hash(data):
        return Identity.full_hash(data)[:16]

    @staticmethod
    def get_random_hash():
        return os.urandom(16)

    @staticmethod
    def validate_announce(packet, only_validate_signature=False):
        return Identity.validate_result

    @staticmethod
    def recall(dest_hash, **kw):
        return Identity.known.get(dest_hash)

    @staticmethod
    def recall_app_data(dest_hash):
        return Identity.app_data.get(dest_hash)


# --------------------------------------------------------------------------
# Fake Destination — just the type/direction/proof constants routing checks.
# (Values mirror urns/const.py.)
# --------------------------------------------------------------------------
class Destination:
    SINGLE = 0x00
    GROUP = 0x01
    PLAIN = 0x02
    LINK = 0x03
    IN = 0x11
    OUT = 0x12
    PROVE_NONE = 0x21
    PROVE_APP = 0x22
    PROVE_ALL = 0x23


def _install_shims():
    # micropython.const (+ no-op native/viper decorators)
    if "micropython" not in sys.modules:
        mp = types.ModuleType("micropython")
        mp.const = lambda x: x
        mp.native = lambda f: f
        mp.viper = lambda f: f
        sys.modules["micropython"] = mp

    sys.modules.setdefault("uhashlib", hashlib)
    sys.modules.setdefault("uasyncio", asyncio)
    sys.modules.setdefault("machine", types.ModuleType("machine"))

    if "ucryptolib" not in sys.modules:
        uc = types.ModuleType("ucryptolib")
        uc.aes = None
        sys.modules["ucryptolib"] = uc

    if "network" not in sys.modules:
        net = types.ModuleType("network")
        net.STA_IF = 0
        net.AP_IF = 1
        net.WLAN = lambda *a, **k: None
        sys.modules["network"] = net


def _register_urns_pkg():
    import importlib.machinery
    import importlib.util

    if "urns" in sys.modules and getattr(sys.modules["urns"], "_test_synthetic", False):
        return  # already set up

    fw = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../firmware
    urns_dir = os.path.join(fw, "urns")
    if not os.path.isdir(urns_dir):
        raise RuntimeError("urns dir not found at " + urns_dir)

    spec = importlib.machinery.ModuleSpec("urns", loader=None, is_package=True)
    spec.submodule_search_locations = [urns_dir]
    pkg = importlib.util.module_from_spec(spec)
    pkg.__path__ = [urns_dir]
    pkg._test_synthetic = True
    sys.modules["urns"] = pkg

    # Inject fakes BEFORE any real import resolves them.
    fid = types.ModuleType("urns.identity")
    fid.Identity = Identity
    sys.modules["urns.identity"] = fid

    fds = types.ModuleType("urns.destination")
    fds.Destination = Destination
    sys.modules["urns.destination"] = fds


def setup():
    _install_shims()
    _register_urns_pkg()
    import importlib
    const = importlib.import_module("urns.const")
    log = importlib.import_module("urns.log")
    packet = importlib.import_module("urns.packet")
    transport = importlib.import_module("urns.transport")
    # Quiet logs during tests unless TEST_VERBOSE is set.
    if not os.environ.get("TEST_VERBOSE"):
        log.set_loglevel(log.LOG_NONE)
    return const, log, packet, transport


const, log, packet, transport = setup()
Transport = transport.Transport

import importlib as _importlib
link = _importlib.import_module("urns.link")        # module-level only (crypto is lazy)
parse_signalling = link._parse_signalling


class MockInterface:
    """Captures every raw it is asked to transmit (process_outgoing)."""

    def __init__(self, name="mock", online=True, mode=None, hw_mtu=500,
                 bitrate=10000, out=True, in_=True):
        self.name = name
        self.online = online
        self.OUT = out
        self.IN = in_
        self.HW_MTU = hw_mtu
        self.bitrate = bitrate
        self.mode = mode
        # IFAC disabled so Transport._ifac_validate passes packets through.
        self.ifac_signing_key = None
        self.ifac_key = None
        self.ifac_size = 0
        self.rssi = None
        self.snr = None
        self.sent = []   # list[bytes]

    def process_outgoing(self, data):
        self.sent.append(bytes(data))
        return True

    def __str__(self):
        return self.name


# --------------------------------------------------------------------------
# Raw-packet builders (hand-crafted wire bytes; no crypto needed).
# Flags byte: (header<<6)|(ctxflag<<5)|(transport_type<<4)|(dest_type<<2)|ptype
# --------------------------------------------------------------------------
def _flags(header, transport_type, dest_type, ptype, ctx_flag=0):
    return (header << 6) | (ctx_flag << 5) | (transport_type << 4) | (dest_type << 2) | ptype


def build_announce_hdr1(dest_hash, data=b"\x00" * 64, hops=0, dest_type=0x00):
    # PKT_ANNOUNCE=0x01, HDR_1=0, BROADCAST=0
    flags = _flags(0, 0, dest_type, 0x01)
    return bytes([flags, hops]) + dest_hash + bytes([0x00]) + data


def build_announce_hdr2(transport_id, dest_hash, data=b"\x00" * 64, hops=1, dest_type=0x00):
    # HDR_2=1, TRANSPORT=1
    flags = _flags(1, 1, dest_type, 0x01)
    return bytes([flags, hops]) + transport_id + dest_hash + bytes([0x00]) + data


def build_data_hdr2(transport_id, dest_hash, ciphertext=b"\x00" * 16, hops=0,
                    context=0x00, dest_type=0x00):
    # PKT_DATA=0x00, HDR_2=1, TRANSPORT=1
    flags = _flags(1, 1, dest_type, 0x00)
    return bytes([flags, hops]) + transport_id + dest_hash + bytes([context]) + ciphertext


def build_data_hdr1(dest_hash, ciphertext=b"\x00" * 16, hops=0, context=0x00, dest_type=0x00):
    flags = _flags(0, 0, dest_type, 0x00)
    return bytes([flags, hops]) + dest_hash + bytes([context]) + ciphertext


def build_announce_data(emitted=1000, pubkey=None, name_hash=None, extra=b""):
    """Construct an announce payload: public_key(64) + name_hash(10) +
    random_hash(10) [+ extra]. random_hash = 5 random + 5 big-endian seconds,
    so Transport._announce_emitted() recovers `emitted`."""
    pk = pubkey if pubkey is not None else b"\x11" * 64
    nh = name_hash if name_hash is not None else b"\x22" * 10
    random_hash = b"\x33" * 5 + int(emitted).to_bytes(5, "big")
    return pk + nh + random_hash + extra


def build_proof(dest_hash, sig=b"\x00" * 64, hops=0):
    # PKT_PROOF=0x03, HDR_1, BROADCAST, dest_type SINGLE. dest_hash = the
    # truncated hash of the proven packet (a ProofDestination hash).
    flags = _flags(0, 0, 0x00, 0x03)
    return bytes([flags, hops]) + dest_hash + bytes([0x00]) + sig


def build_linkrequest_hdr2(transport_id, dest_hash, eph_pub=None, mtu=500, mode=0x01,
                           hops=0, with_signalling=True):
    # LINKREQUEST payload = X25519_pub(32) + Ed25519_pub(32) [+ signalling(3)].
    pub = eph_pub if eph_pub is not None else (b"\x44" * 32 + b"\x55" * 32)
    data = pub + (link._signalling_bytes(mtu, mode) if with_signalling else b"")
    flags = _flags(1, 1, 0x00, 0x02)   # HDR_2, TRANSPORT, SINGLE, PKT_LINKREQUEST
    return bytes([flags, hops]) + transport_id + dest_hash + bytes([0x00]) + data


def build_lrproof(link_id, sig=b"\x00" * 64, eph_pub=b"\x44" * 32, signalling=b"", hops=0):
    # Link-request proof: PKT_PROOF + CTX_LRPROOF(0xFF), addressed to link_id (DEST_LINK), HDR_1.
    data = sig + eph_pub + signalling
    flags = _flags(0, 0, 0x03, 0x03)   # HDR_1, BROADCAST, DEST_LINK, PKT_PROOF
    return bytes([flags, hops]) + link_id + bytes([0xFF]) + data


class _FakeId:
    def __init__(self, h):
        self.hash = h


def set_identity(h=b"\xAA" * 16):
    Transport.identity = _FakeId(h)
    return Transport.identity


def reset_transport():
    """Clear all routing tables between tests."""
    T = Transport
    T.interfaces = []
    T.destinations = []
    T.announce_handlers = []
    T.active_links = []
    T.pending_links = []
    T.receipts = []
    T.packet_hashlist = set()
    T.packet_hashlist_prev = set()
    T.path_table = {}
    T.reverse_table = {}
    T.link_table = {}
    T.packet_cache = {}
    T.path_states = {}
    T.announce_table = {}
    T.reachable_destinations = {}
    T.discovery_path_requests = {}
    T.blackholed_identities = []
    T._announce_rate = {}
    T._pr_tags = []
    T.relayed_announces = T.relayed_data = T.relayed_links = T.relayed_proofs = 0
    T.control_destinations = []
    T.control_hashes = []
    T.persist_path = None
    T._last_cull = 0
    T._last_persist = 0
    T.transport_enabled = True
    T.strict_lr_validation = False    # host rig has no native crypto; test forwarding
    set_identity()
    Identity.validate_result = True
    Identity.known = {}
    Identity.app_data = {}
