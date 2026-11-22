# Reticulum 1.6 stamps (host-side): fixed-point cost helpers vs the reference,
# the pure-Python link-request key search, announce-extension parsing against
# REAL 1.6.1 announces, and OutgoingLink's stamped link request.
#
# The constants below were generated with reference RNS 1.6.1 (tag 1.6.1,
# PYTHONPATH override): target_from_cost() for a spread of costs, and announces
# built with Destination.announce(send=False) with STAMP_DESTINATIONS on,
# Transport.DESTINATION_VALUE=4.0 (so the destination stamp is instant) and
# set_link_cost(); FULL_RAW used the reference defaults (value 25, link cost
# 17.8 — a ~2 min multicore search) so it validates at the port's default
# required cost of 23.5. Tests run under plain python3, no RNS needed.
#
# Run:  python3 firmware/tests/test_stamps.py

import sys
import os
import hashlib
import asyncio
import importlib
import importlib.util

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
from harness import const, packet, Transport, MockInterface, reset_transport, set_identity, link

stamps = importlib.import_module("urns.stamps")

TAB_DIGEST = '55ba3ce1df224895b0dfdc102eb4359fc2f5a7efefee68df0cc90c8a8ac24f48'
REF_TARGETS = {
    0.0: 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
    3.25: '1ae89f9958000000000000000000000000000000000000000000000000000000',
    12.5: '000b504f333c0000000000000000000000000000000000000000000000000000',
    15.8: '000125e857118000000000000000000000000000000000000000000000000000',
    17.8: '0000497a15c46000000000000000000000000000000000000000000000000000',
    23.5: '0000016a09e66780000000000000000000000000000000000000000000000000',
    25.0: '0000008000000000000000000000000000000000000000000000000000000000',
    40.0: '0000000001000000000000000000000000000000000000000000000000000000',
    100.0: '0000000000000000000000001000000000000000000000000000000000000000',
}
# name -> (raw announce hex, dest_hash hex, announced app_data hex, link cost, stamp value)
VECTORS = {
    'v1': (
        '010011fb5ce6c3ece1bfa68db5cf6a241a6d0068be47f6e9e0c6d9c6eecc9b46900b276bb461ca9bdcb92b3794eef622'
        '078f00691804e793c16860aa028c5e9004e19865b6d2564f3a8d128303d14ca3fed7ae1841ab6263c2de42733f71a637'
        '76df006b01a135fc89ee1a1774a0dbe64be0a5a78d30adb624689dcde0819ca128752edffdbb34ae4a6702b4da395a9e'
        '492b1b772dde6c212169f381f214a22144dc776afce903c100b8b0b842bace94e434ba05212e2a800c'
        , '11fb5ce6c3ece1bfa68db5cf6a241a6d', '', 12.5, 4.3359375),
    'v2': (
        '2100893fae8e86b8ed9a26260274e7b59def00c22f99f70c5d0b3b344f8e6767d05d058e47bbdb0815df5ac98680c95e'
        '86646132929fff197ce61867e6e8cc7b05894c61c39da2de6b9d549bd13309ece5e222fce1b3220214901f3f359cd09c'
        'db5c006b01a1356200d9467903bcdf8d1d4471b541227b625df423f08c59f9aac4eaa98382dc39abdad3823535237461'
        '94a4d040c9bdb4867a83661c6c44e1a8419bf59e7c580924948c68d0b5edaf9edc42639788d95f3af3ccc3e8c57b1cd6'
        '8b1bfdd8c49f0268656c6c6f2d6170702d64617461c1006ac63fa250a8711583773391f8b6cd11'
        , '893fae8e86b8ed9a26260274e7b59def', '68656c6c6f2d6170702d64617461', 17.8, 4.07421875),
    'v3': (
        '01000e4c54a532cb1bc2e46207930e174402001401973ac731c70f541d326f45bb876faf50cb2fd522416c0dd882d339'
        '515878b82e2cfaaf467a975b251fca93bf54ce6c1eb50f4f21ea5f6b09519b80e7453a54064c0ac2eb99d77edf2f10fc'
        '0d61006b01a135f9cab8046ad75b09cd0860e80e530c765a8d3429c1b6e05e2b4918ceb8a3d6032d29d7d61bff0594cf'
        '6d592c74d1c917a68959bbbe436c192db39896bfb6fd0392c4046e616d65c0c1005e7c43fd03737f0f5309859ee03d00'
        '00'
        , '0e4c54a532cb1bc2e46207930e174402', '92c4046e616d65c0', 0, 5.48046875),
    'v4': (
        '01007b93cffb0ca2bb9b6d451d7303eaaa8e00f19982746328af94b690ddaa849ad5d7b542bc9a1ce9c06c03839f86b2'
        '32ff0d5ba1f39874df7b533d81f1e030d6039f7b3ec54de232331e6a3ca912af4d8a2dcf6a6a0c340e981aa13ff6a188'
        '9536006b01a135236181bd4d6ac1d9d70bfd0500c925aa2d1ee8954c6c8eebc9d99daa810ac389886c878c29a0ffec3a'
        '761e195d9ed31dd40fd1d2ad87c6313067009807914e03c100a6cba94281d3e572e53301f1a2820028'
        , '7b93cffb0ca2bb9b6d451d7303eaaa8e', '', 40, 4.1171875),
    'v5': (
        '0100ce2f0216f705c7c97c4e4d7d9d0f189600fb3849f6a788f6ec79b44de4eea889f28765fef98616e3e923cf4f0ec2'
        'fc687f79dcd7ea934e4a9b328712e6f1012dbb966f0dc4c59e619a5452d2ea84e5393e966d5a16975356f54468313f14'
        '47dc006b01a1356d68e087624ec2b7ad3120ca47f494445964d5958eeac6c3ffbde18168fd0ab722e424d49d79704a67'
        '4cdc9e56ea4f778780a0938f61bee57f3257183d9d440c78787878787878787878787878787878787878787878787878'
        '787878787878787878787878787878c100e2ed51008a1d9b950d48fa32f9464003'
        , 'ce2f0216f705c7c97c4e4d7d9d0f1896', '78787878787878787878787878787878787878787878787878787878787878787878787878787878', 3.25, 7.671875),
    'plain': (
        '01009fe09df8d5afe78008493fc16d46b4b200104e9058efeaa892af4045ebfc907879408a11c5a93592ed32cb0e2c8c'
        '50f33818c22556bd322546854d8eba577b4965f22ceef82633413a568e8f83c899db676ec645778632bb62fad4a231f1'
        '6bf2006b01a1351195967f635bd4679c9b87ce6efe96ed64d747f5dfc358f862d859f8c95e206e1be355d1c55e69b366'
        '98f15b716044c0b628f0d3eace6fae6bfc93901d0ca303706c61696e20617070206461746120776974686f757420616e'
        '79207374616d7020657874656e73696f6e2c206c6f6e67'
        , '9fe09df8d5afe78008493fc16d46b4b2', '706c61696e20617070206461746120776974686f757420616e79207374616d7020657874656e73696f6e2c206c6f6e67', None, None),
    'fake': (
        '01006913d6a77194cf282c4b6397be8558ff00f2767498c22aae72e57eca0c76c831ab1eab9a9d06a25a387d3beea1a5'
        '63985fbfc5c0af3466284b29c24d2e3ad5f5baf674ad9ada6201fb89ad0994eb4744fcf55eda7b11f92406265993e56a'
        'f021006b01a194e8b9db9ced21a76ba145173efa2f1d85a2f9ef540631d7ad25b1154b04aae97b9b3b79c7122c6ef06a'
        '0fa7211a4cd61a9721ce9106ec33e8e7a36dc9378ac30ac10102030405060708090a0b0c0d0e0f1011'
        , '6913d6a77194cf282c4b6397be8558ff', 'c10102030405060708090a0b0c0d0e0f1011', None, None),
}
# Generated at the DEFAULT required cost (23.5; the destination used value 25, stamp value 26.33984375), link cost 17.8, app_data b'default-cost'
FULL_RAW = (
    '0100dcfb9188969de78f442314d582b9576100b96a668d555522e3589b6704f699ec40e02bd8731903c6635fcdd4b5fc'
    '1405361a391b316a32b25cc38c6b03fe66880d965f5e8998848ebe19597cf880d5c2356bb45676b29334358f535b0b50'
    'a00e006b01a0974a66439bd17901e23b4f44872ee670782607af30db6a127f644a70edc8acbf1b048848918b5af24a5c'
    'd81db5ce73da32f642f6d48cccd47c721ffc57ab08d00664656661756c742d636f7374c10023a49ea70ec77f82ce9836'
    'aead35cd11'
)
FULL_DEST = 'dcfb9188969de78f442314d582b95761'
FULL_STAMP_VALUE = 26.33984375


def sha(b):
    return hashlib.sha256(b).digest()


# --------------------------------------------------------------------------
# Fixed-point helpers
# --------------------------------------------------------------------------
def _tab_values():
    return [int.from_bytes(stamps._TAB[i * 5:i * 5 + 5], "big") for i in range(256)]


def test_tab_digest_matches_reference():
    tab = _tab_values()
    assert len(tab) == 256 and len(stamps._TAB) == 1280
    assert tab[0] == 1 << 33
    assert all(b > a for a, b in zip(tab, tab[1:]))
    assert hashlib.sha256(",".join(str(v) for v in tab).encode("ascii")).hexdigest() == TAB_DIGEST


def test_target_from_fp_matches_reference():
    for cost, want in REF_TARGETS.items():
        got = stamps.target_from_fp(stamps.cost_to_fp(cost))
        assert len(got) == 32
        assert got.hex() == want, (cost, got.hex(), want)


def test_cost_helpers():
    assert stamps.cost_to_fp(12.5) == 3200
    assert stamps.cost_to_fp(17.8) == 4557          # floor(17.8*256 + .5)
    assert stamps.cost_to_fp(0) == 0 and stamps.cost_to_fp(-3) == 0
    assert stamps.cost_to_fp(1000) == 100 * 256     # reference STAMP_COST_CAP


def test_stamp_valid_boundary():
    mat = b"m" * 48
    target = stamps.target_from_fp(stamps.cost_to_fp(3.25))
    # find one stamp on each side of the boundary
    ok = bad = None
    i = 0
    while ok is None or bad is None:
        s = i.to_bytes(4, "big")
        if sha(mat + s) <= target:
            ok = ok or s
        else:
            bad = bad or s
        i += 1
    assert stamps.stamp_valid(ok, 3.25, mat) and not stamps.stamp_valid(bad, 3.25, mat)
    assert stamps.stamp_valid(bad, 0, mat)           # cost 0 accepts everything


def test_stamp_value_matches_reference_values():
    # The reference computed each announce's stamp value with RNS.Stamps.stamp_value.
    for name in ("v1", "v2", "v3", "v4", "v5"):
        raw, dest, _app, _lr, value = VECTORS[name]
        pkt = _unpack(raw)
        data = pkt.data
        ext = _app_data_of(pkt)[-18:]
        material = pkt.destination_hash + data[:64] + data[64:74]
        got = stamps.stamp_value(material, ext[2:16])
        assert got == value, (name, got, value)


# --------------------------------------------------------------------------
# Key search
# --------------------------------------------------------------------------
def _force_pure():
    stamps.set_table_size(64)
    stamps._native_state = -1
    stamps.reset_search()


def _check_key(dest, sig_pub, cost, prv, pub):
    from urns.crypto import X25519PrivateKey
    assert len(prv) == 32 and len(pub) == 32
    key = X25519PrivateKey.from_private_bytes(prv)
    assert key.public_key().public_bytes() == pub, "port X25519 disagrees with the search"
    assert key.private_bytes() == prv, "scalar not in clamped form"
    target = stamps.target_from_fp(stamps.cost_to_fp(cost))
    assert sha(dest + sig_pub + pub) <= target
    assert stamps.stamp_valid(pub, cost, dest + sig_pub)


def test_find_link_key_pure_python():
    _force_pure()
    assert not stamps.have_native()
    for cost in (3.25, 8, 10):
        dest, sig = os.urandom(16), os.urandom(32)
        tried_total = 0
        prv = None
        calls = 0
        while prv is None:                    # sliced: one 32-candidate batch per call
            prv, pub, tried = stamps.find_link_key(dest, sig, cost, 32)
            tried_total += tried
            calls += 1
            assert calls < 20000
        assert tried_total >= 1
        _check_key(dest, sig, cost, prv, pub)


def test_find_link_key_cost_zero_first_candidate():
    _force_pure()
    dest, sig = os.urandom(16), os.urandom(32)
    prv, pub, tried = stamps.find_link_key(dest, sig, 0, 32)
    while prv is None:                    # the first calls only build the search state (sliced start-up)
        assert tried == 0
        prv, pub, tried = stamps.find_link_key(dest, sig, 0, 32)
    assert prv is not None and tried == 1
    _check_key(dest, sig, 0, prv, pub)


def test_find_link_key_no_key_returns_tried():
    _force_pure()
    dest, sig = os.urandom(16), os.urandom(32)
    prv, pub, tried = stamps.find_link_key(dest, sig, 60, 64)   # hopeless in 64 candidates
    while tried == 0:                     # start-up slices
        prv, pub, tried = stamps.find_link_key(dest, sig, 60, 64)
    assert prv is None and pub is None and tried == 64


class _FakeNative:
    """Stands in for the ed25519_fast natmod: checks the call contract and does
    the search with the pure-Python implementation."""
    def __init__(self):
        self.inits = 0
        self.calls = 0

    def x25519_stamp2_init(self, buf):
        assert isinstance(buf, bytearray)
        assert len(buf) == 16 + 120 * (stamps.TABLE_N + 1)
        self.inits += 1
        return stamps.TABLE_N

    def x25519_stamp2(self, seed, material, target, max_candidates, buf):
        assert len(seed) == 32 and len(target) == 32 and isinstance(buf, bytearray)
        self.calls += 1
        s = stamps._PySearch()
        prv, pub, tried, _ = s.run(material, target, max_candidates)
        return (prv, pub, tried)


def test_native_path_buffer_allocated_once():
    from urns.crypto import ed25519
    fake = _FakeNative()
    old = getattr(ed25519, "_native", None)
    ed25519._native = fake
    try:
        stamps.set_table_size(16)
        assert stamps.have_native() and stamps.slice_size() == stamps.SLICE_NATIVE
        dest, sig = os.urandom(16), os.urandom(32)
        for _ in range(2):
            prv, pub, tried = stamps.find_link_key(dest, sig, 3.25, 5000)
            assert prv is not None
            _check_key(dest, sig, 3.25, prv, pub)
        assert fake.inits == 1 and fake.calls == 2
        buf = stamps._nbuf
        stamps.find_link_key(dest, sig, 3.25, 5000)
        assert stamps._nbuf is buf, "table buffer must be kept for the life of the program"
        assert len(buf) == 16 + 120 * 17, "configurable table size"
    finally:
        ed25519._native = old
        _force_pure()


def test_old_native_module_without_stamp2_falls_back():
    from urns.crypto import ed25519

    class Old:
        pass

    old = getattr(ed25519, "_native", None)
    ed25519._native = Old()
    try:
        stamps.set_table_size(64)
        assert not stamps.have_native()
        assert stamps.slice_size() == stamps.SLICE_PY
    finally:
        ed25519._native = old
        _force_pure()


# --------------------------------------------------------------------------
# Announce extension (real Identity under the harness)
# --------------------------------------------------------------------------
_spec = importlib.util.spec_from_file_location(
    "urns.identity_real", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "urns", "identity.py"))
ident_mod = importlib.util.module_from_spec(_spec)
sys.modules["urns.identity_real"] = ident_mod
_spec.loader.exec_module(ident_mod)
RealIdentity = ident_mod.Identity


def _unpack(raw_hex):
    p = packet.Packet(None, bytes.fromhex(raw_hex))
    assert p.unpack()
    return p


def _app_data_of(pkt):
    """App data region of a reference announce as the port sees it."""
    base = 64 + 10 + 10
    if pkt.context_flag == const.FLAG_SET:
        base += 32
    return pkt.data[base + 64:]


def _fresh(required=4.0):
    RealIdentity.known_destinations.clear()
    RealIdentity.known_ratchets.clear()
    ident_mod.ANNOUNCE_STAMP_COST = required


def test_default_required_cost_is_23_5():
    assert ident_mod.ANNOUNCE_STAMP_COST == 23.5
    assert ident_mod.ANNOUNCE_STAMPS is True


def test_reference_announces_stripped_and_cost_recorded():
    for name in ("v1", "v2", "v3", "v4", "v5"):
        raw, dest_hex, app_hex, lr, _val = VECTORS[name]
        _fresh()
        pkt = _unpack(raw)
        dest = bytes.fromhex(dest_hex)
        assert RealIdentity.validate_announce(pkt) is True, name   # full Ed25519 verify (no cache)
        want = bytes.fromhex(app_hex) or None
        assert RealIdentity.recall_app_data(dest) == want, (name, RealIdentity.recall_app_data(dest))
        assert RealIdentity.recall_link_cost(dest) == stamps.cost_to_fp(lr) / 256, (name, RealIdentity.recall_link_cost(dest))
        if name == "v2":
            assert pkt.context_flag == const.FLAG_SET       # ratchet announce
            assert dest in RealIdentity.known_ratchets
        # Fast path (destination now known): same result, signature not re-verified.
        assert RealIdentity.validate_announce(_unpack(raw)) is True
        assert RealIdentity.recall_app_data(dest) == want
        assert RealIdentity.recall_link_cost(dest) == stamps.cost_to_fp(lr) / 256


def test_signature_covers_the_unstripped_extension():
    raw, dest_hex, _a, _l, _v = VECTORS["v2"]
    b = bytearray(bytes.fromhex(raw))
    for off in (-1, -2, -10, -18, -19):     # cost anchor, fraction, stamp, marker, plain app_data
        t = bytearray(b)
        t[off] ^= 0x01
        _fresh()
        p = packet.Packet(None, bytes(t))
        assert p.unpack()
        assert RealIdentity.validate_announce(p) is False, off
        assert bytes.fromhex(dest_hex) not in RealIdentity.known_destinations
    _fresh()
    assert RealIdentity.validate_announce(_unpack(raw), only_validate_signature=True) is True


def test_insufficient_stamp_not_stripped():
    raw, dest_hex, _a, lr, value = VECTORS["v5"]      # stamp value ~7.7, announced lr cost 3.25
    assert value < 12
    _fresh(required=12.0)
    pkt = _unpack(raw)
    dest = bytes.fromhex(dest_hex)
    assert RealIdentity.validate_announce(pkt) is True      # still a valid announce
    ad = RealIdentity.recall_app_data(dest)
    assert ad == _app_data_of(pkt) and len(ad) == 40 + 18, "extension must be left in place"
    assert RealIdentity.recall_link_cost(dest) is None


def test_plain_and_fake_extension_app_data_untouched():
    for name in ("plain", "fake"):
        raw, dest_hex, app_hex, _lr, _v = VECTORS[name]
        _fresh()
        assert RealIdentity.validate_announce(_unpack(raw)) is True
        dest = bytes.fromhex(dest_hex)
        assert RealIdentity.recall_app_data(dest) == bytes.fromhex(app_hex)
        assert RealIdentity.recall_link_cost(dest) is None
    # "fake" has the 0xC1 marker exactly 18 bytes from the end but no valid stamp.
    assert bytes.fromhex(VECTORS["fake"][2])[-18] == 0xC1


def test_default_cost_announce_validates_with_unmodified_constant():
    _fresh(required=23.5)
    pkt = _unpack(FULL_RAW)
    dest = bytes.fromhex(FULL_DEST)
    assert RealIdentity.validate_announce(pkt) is True
    assert RealIdentity.recall_app_data(dest) == b"default-cost"
    assert RealIdentity.recall_link_cost(dest) == 4557 / 256       # 17.8 quantised to 8.8 fixed point
    assert FULL_STAMP_VALUE >= 23.5


def test_split_ext_edge_cases():
    raw, dest_hex, _a, _l, _v = VECTORS["v1"]
    pkt = _unpack(raw)
    ext = _app_data_of(pkt)
    assert len(ext) == 18
    dest = pkt.destination_hash
    pk, nh = pkt.data[:64], pkt.data[64:74]
    ident_mod.ANNOUNCE_STAMP_COST = 4.0
    ok = ident_mod._split_ext(dest, pk, nh, b"abc" + ext)
    assert ok == (b"abc", 3200)
    assert ident_mod._split_ext(dest, pk, nh, ext) == (None, 3200)
    # extended flag set -> format unknown, untouched
    ext_x = ext[:-1] + bytes([ext[-1] | 0x80])
    assert ident_mod._split_ext(dest, pk, nh, ext_x) == (ext_x, None)
    # cost above the 100 cap -> untouched
    ext_c = ext[:-2] + bytes([0, 101])
    assert ident_mod._split_ext(dest, pk, nh, ext_c) == (ext_c, None)
    # wrong marker / too short
    ext_m = bytes([0xC0]) + ext[1:]
    assert ident_mod._split_ext(dest, pk, nh, ext_m) == (ext_m, None)
    assert ident_mod._split_ext(dest, pk, nh, ext[1:]) == (ext[1:], None)
    # switch off
    ident_mod.ANNOUNCE_STAMPS = False
    try:
        assert ident_mod._split_ext(dest, pk, nh, ext) == (ext, None)
    finally:
        ident_mod.ANNOUNCE_STAMPS = True


def test_known_destination_entry_shape_and_persistence_roundtrip():
    import json
    import tempfile
    raw, dest_hex, _a, _l, _v = VECTORS["v1"]
    _fresh()
    assert RealIdentity.validate_announce(_unpack(raw))
    dest = bytes.fromhex(dest_hex)
    assert len(RealIdentity.known_destinations[dest]) == 5
    old = RealIdentity.storagepath
    with tempfile.TemporaryDirectory() as d:
        RealIdentity.storagepath = d
        RealIdentity.save_known_destinations()
        RealIdentity.known_destinations.clear()
        RealIdentity.load_known_destinations()
        assert RealIdentity.recall_link_cost(dest) == 12.5
        # a file written before link costs existed (4-element rows) still loads
        data = json.load(open(d + "/known_destinations.json"))
        for k in data:
            data[k] = data[k][:4]
        json.dump(data, open(d + "/known_destinations.json", "w"))
        RealIdentity.known_destinations.clear()
        RealIdentity.load_known_destinations()
        assert RealIdentity.recall_link_cost(dest) is None
    RealIdentity.storagepath = old


# --------------------------------------------------------------------------
# OutgoingLink
# --------------------------------------------------------------------------
DEST = b"\xD2" * 16


class _Dest:
    def __init__(self, h=DEST):
        self.hash = h
        self.hexhash = h.hex()
        self.type = const.DEST_SINGLE


def _link_setup():
    iface = MockInterface("tcp", hw_mtu=500, bitrate=10_000_000)
    reset_transport()
    set_identity()
    Transport.interfaces = [iface]
    import time
    Transport.path_table[DEST] = [time.time(), DEST, 1, time.time() + 3600, iface, None, 0]
    return iface


def _request_parts(raw):
    # HDR_1 LINKREQUEST: flags, hops, dest(16), context, then pub(32)+sigpub(32)+signalling(3)
    data = raw[19:]
    assert len(data) == 67
    return data[:32], data[32:64]


def test_outgoing_link_without_cost_is_unchanged():
    _force_pure()
    iface = _link_setup()
    ol = link.OutgoingLink(_Dest())
    assert len(iface.sent) == 1, "request goes out before the constructor returns"
    assert ol._stamp_deadline is None and ol._stamp_cost is None
    assert ol in Transport.pending_links and ol.link_id != b"\x00" * 16
    # cost 0 behaves the same
    iface2 = _link_setup()
    harness.Identity.link_costs[DEST] = 0
    ol2 = link.OutgoingLink(_Dest())
    assert len(iface2.sent) == 1 and ol2._stamp_deadline is None


def test_outgoing_link_with_cost_sync_fallback():
    _force_pure()
    iface = _link_setup()
    harness.Identity.link_costs[DEST] = 8.0
    ol = link.OutgoingLink(_Dest())           # no running loop -> searched inline
    assert len(iface.sent) == 1
    pub, sig = _request_parts(iface.sent[0])
    assert stamps.stamp_valid(pub, 8.0, DEST + sig)
    assert sha(DEST + sig + pub) <= stamps.target_from_fp(stamps.cost_to_fp(8.0))
    assert ol._pub_bytes == pub and ol._sig_pub_bytes == sig
    assert ol.status == link.OutgoingLink.PENDING and ol._stamp_deadline is None
    assert ol.link_id != b"\x00" * 16 and ol in Transport.pending_links and ol._stamp_tried >= 1


def test_outgoing_link_with_cost_async_yields_between_slices():
    _force_pure()
    iface = _link_setup()
    harness.Identity.link_costs[DEST] = 10.0
    ticks = []

    async def main():
        async def ticker():
            while True:
                ticks.append(1)
                await asyncio.sleep(0)
        t = asyncio.create_task(ticker())
        ol = link.OutgoingLink(_Dest())
        assert len(iface.sent) == 0, "no request until the key is found"
        assert ol.status == link.OutgoingLink.PENDING and ol._stamp_deadline is not None
        assert ol in Transport.pending_links
        n = 0
        while not iface.sent and n < 100000:
            await asyncio.sleep(0)
            n += 1
        t.cancel()
        return ol

    ol = asyncio.run(main())
    assert len(iface.sent) == 1
    pub, sig = _request_parts(iface.sent[0])
    assert stamps.stamp_valid(pub, 10.0, DEST + sig)
    # one yield per slice (a lucky early hit means few slices, so scale by what was tried)
    slices = (ol._stamp_tried + stamps.SLICE_PY - 1) // stamps.SLICE_PY
    assert len(ticks) >= slices - 1, "other tasks must run between slices"
    # establishment clock started when the request went out
    import time
    assert time.time() - ol.request_time < 5 and ol._stamp_deadline is None


def test_outgoing_link_stamp_timeout_closes_through_callback():
    _force_pure()
    iface = _link_setup()
    harness.Identity.link_costs[DEST] = 90.0          # hopeless (allowed: exercises the timeout)
    link.OutgoingLink.max_stamp_cost = 100.0
    closed = []

    async def main():
        ol = link.OutgoingLink(_Dest(), closed_callback=lambda l: closed.append(l), stamp_timeout=0.3)
        for _ in range(100000):
            await asyncio.sleep(0.01)
            if closed:
                break
        return ol

    try:
        ol = asyncio.run(main())
    finally:
        link.OutgoingLink.max_stamp_cost = None
    assert closed and ol.status == link.OutgoingLink.CLOSED and ol.close_reason == "stamp"
    assert iface.sent == [], "no link request for a failed stamp"


def test_stamp_deadline_serviced_by_job_loop_check():
    import time
    _force_pure()
    iface = _link_setup()
    harness.Identity.link_costs[DEST] = 90.0
    link.OutgoingLink.max_stamp_cost = 100.0
    closed = []

    async def main():
        ol = link.OutgoingLink(_Dest(), closed_callback=lambda l: closed.append(l))
        assert not closed
        ol._stamp_deadline = time.time() - 1          # budget used up while the loop was starved
        ol.check_timeout()
        return ol

    try:
        ol = asyncio.run(main())
    finally:
        link.OutgoingLink.max_stamp_cost = None
    assert closed and ol.status == link.OutgoingLink.CLOSED


def test_default_stamp_timeout_is_120s():
    assert link.OutgoingLink.STAMP_TIMEOUT == 120


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print("PASS", t.__name__)
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print("FAIL", t.__name__, repr(e))
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
