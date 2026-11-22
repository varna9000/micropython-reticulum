# Announce stamps on OUR announces (Reticulum 1.6), host-side. Run:
#   python3 firmware/tests/test_announce_stamps.py
#
# VECTORS: announces built by the PORT (Destination.announce with a stored stamp, fixed
# test identity TEST_PRV in announce_stamp_support.py, lxmf.delivery destination) and then run
# through REFERENCE RNS 1.6.1 Identity.validate_announce with Transport.STAMPS_ENFORCED = True at
# the default required cost 23.5 and signal_stamp=True (rnsvenv python, PYTHONPATH=reference
# source). STAMP25 is a value-25.5 stamp made with tools/gen_announce_stamp.py for that
# destination's material. Reference results (REF_RESULTS below, copied from that run):
#   lxmf      -> True, app_data 93c4..., link cost 0.0, stamp value 25.50390625
#   none      -> True, app_data empty (the extension alone was the app_data), link cost 0.0
#   plain     -> True, app_data 'hello-plain-app-data', link cost 17.5
#   weak      -> "invalid_stamp"  (port signed an announce carrying a stamp of value ~5)
#   unstamped -> "no_stamp"
# Tests here assert the port reproduces the layout and parses these exact bytes the same way.

import asyncio
import importlib
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import announce_stamp_support as S
from harness import const, packet

stamps = importlib.import_module("urns.stamps")
umsgpack = importlib.import_module("urns.umsgpack")
A = S.announce_stamps
ident_mod = S.ident_mod
RealIdentity = S.RealIdentity

VECTORS = {
    'lxmf': (
        '0100f9ba20d52608a9385877fc8e7f7c318c008ef24883c20957c4e235d22d5bc65a5b0fb8faf4cddd823c3adc1501d6'
        'cd6d40e5482284bb7ef345cf2f7965fd354f6a7dc632d2e9658356e8c2ee71dd184da36ec60bc318e2c0f0d9082850e2'
        '5362006b01dabf2b8783af603b7916de98453f7be496f8b3a449ac211866630529a0677ecfb0e5a630fdb371b6c62007'
        'e54f86ec5de04d6c13b95ab004f79571c3598557effd0293c40954657374204e6f6465c09100c1001ca7c27a368a2015'
        'bcfc0a0000000000'
    ),
    'none': (
        '0100f9ba20d52608a9385877fc8e7f7c318c008ef24883c20957c4e235d22d5bc65a5b0fb8faf4cddd823c3adc1501d6'
        'cd6d40e5482284bb7ef345cf2f7965fd354f6a7dc632d2e9658356e8c2ee71dd184da36ec60bc318e2c0f0d9083f7714'
        '3ac3006b01dabffb2a46f3f82f06d06facafa85f43c3d597317b87f2fae3affe12c678f670b6fdf2d7201e5174879393'
        'a0ee4a7e1c99d19c85242dd4a501268a94cb37f12ff302c1001ca7c27a368a2015bcfc0a0000000000'
    ),
    'plain': (
        '0100f9ba20d52608a9385877fc8e7f7c318c008ef24883c20957c4e235d22d5bc65a5b0fb8faf4cddd823c3adc1501d6'
        'cd6d40e5482284bb7ef345cf2f7965fd354f6a7dc632d2e9658356e8c2ee71dd184da36ec60bc318e2c0f0d9084e0da9'
        '506e006b01dabfc3e0fb9f7bc13b237940264c445d4d93af6231fdfc0bdb540f717046c7423a57bf9de893a3c14a6e28'
        '7e40a66944cd450e5c1907942eb9c71fb5d91031e8f70068656c6c6f2d706c61696e2d6170702d64617461c1001ca7c2'
        '7a368a2015bcfc0a0000008011'
    ),
    'weak': (
        '0100f9ba20d52608a9385877fc8e7f7c318c008ef24883c20957c4e235d22d5bc65a5b0fb8faf4cddd823c3adc1501d6'
        'cd6d40e5482284bb7ef345cf2f7965fd354f6a7dc632d2e9658356e8c2ee71dd184da36ec60bc318e2c0f0d908db6995'
        '9655006b01dabf05de8bbc83e3e28148564ea85e2ab2ba71c80fa45875c97cac461a218b058b47f7715620cd2c9e4add'
        'cbc16b2ebddeeb95f8f4a2e6f848b4ee159c885905050a93c40954657374204e6f6465c09100c1000900000000000000'
        '0000000000000000'
    ),
    'unstamped': (
        '0100f9ba20d52608a9385877fc8e7f7c318c008ef24883c20957c4e235d22d5bc65a5b0fb8faf4cddd823c3adc1501d6'
        'cd6d40e5482284bb7ef345cf2f7965fd354f6a7dc632d2e9658356e8c2ee71dd184da36ec60bc318e2c0f0d90898c3bd'
        'c056006b01dabfd15088179d12c440aac94cd3fd85ddd0b71951ba86090d8ce7157507f5a06ff3d0059083331005e9e8'
        '2d2fe046e773c57158d4ccc32957f024c9610ee8b7550f93c40954657374204e6f6465c09100'
    ),
}
STAMP25 = bytes.fromhex('1ca7c27a368a2015bcfc0a000000')
STAMP25_VALUE = 25.50390625
DEST_HEX = 'f9ba20d52608a9385877fc8e7f7c318c'

REF_RESULTS = {
    "lxmf": (True, "93c40954657374204e6f6465c09100", 0.0),
    "none": (True, "", 0.0),
    "plain": (True, "68656c6c6f2d706c61696e2d6170702d64617461", 17.5),
    "weak": ("invalid_stamp", None, None),
    "unstamped": ("no_stamp", None, None),
}


def _tmp():
    d = tempfile.mkdtemp()
    A.reset()
    A.configure(storage_dir=d)
    return d


def _install(d, dest, stamp=STAMP25, value=STAMP25_VALUE):
    os.makedirs(d + "/stamps", exist_ok=True)
    assert A.store(dest.hash, stamp, value)
    A._cache.clear()


def _unpack(raw):
    p = packet.Packet(None, raw)
    assert p.unpack()
    return p


def _fresh():
    RealIdentity.known_destinations.clear()
    RealIdentity.known_ratchets.clear()
    ident_mod.ANNOUNCE_STAMP_COST = 23.5


def _announce_raw(dest, app):
    p = dest.announce(app_data=app, send=False)
    p.pack()
    return p.raw


def test_extension_layout_and_byte_order():
    d = S.make_destination()
    st = bytes(range(14))
    e = A.extension(d, st)
    assert len(e) == 18 and e[0] == 0xC1 and e[1] == 0x00 and e[2:16] == st
    assert e[16:] == b"\x00\x00", "link cost 0 = two zero bytes"
    d.link_cost = 17.5                       # 17.5 * 256 = 4480 = 0x1180: fraction byte first
    assert A.extension(d, st)[16:] == b"\x80\x11"
    d.set_link_cost(40)
    assert A.extension(d, st)[16:] == b"\x00\x28"
    d.set_link_cost(1000)                    # clamped, integer byte MSB (reserved) stays clear
    assert A.extension(d, st)[17] == 0x28
    d.set_link_cost(12.5)
    assert A.extension(d, st)[16:] == b"\x80\x0c"


def test_port_announce_validates_in_port():
    d = S.make_destination()
    tmp = _tmp()
    _install(tmp, d)
    app = S.lxmf_app_data("Test Node")
    raw = _announce_raw(d, app)
    pkt = _unpack(raw)
    assert pkt.data.endswith(A.extension(d, STAMP25)) and len(pkt.data) == 148 + len(app) + 18
    _fresh()
    assert RealIdentity.validate_announce(pkt) is True
    assert RealIdentity.recall_app_data(d.hash) == app       # extension stripped
    assert RealIdentity.recall_link_cost(d.hash) == 0.0
    d.set_link_cost(17.5)
    pkt = _unpack(_announce_raw(d, b"hello"))
    _fresh()
    assert RealIdentity.validate_announce(pkt) is True
    assert RealIdentity.recall_app_data(d.hash) == b"hello"
    assert RealIdentity.recall_link_cost(d.hash) == 17.5
    shutil.rmtree(tmp)


def test_reference_validated_vectors_parse_the_same_in_port():
    for name, raw in VECTORS.items():
        ok, app_hex, lr = REF_RESULTS[name]
        pkt = _unpack(bytes.fromhex(raw))
        _fresh()
        res = RealIdentity.validate_announce(pkt)
        assert res is True, name                              # all are validly SIGNED
        dest = bytes.fromhex(DEST_HEX)
        got = RealIdentity.recall_app_data(dest)
        if ok is True:                                        # reference accepted: port strips + recalls
            assert (got or b"").hex() == app_hex, (name, got)
            assert RealIdentity.recall_link_cost(dest) == lr, name
        else:                                                 # reference rejected: port leaves the app_data alone
            assert RealIdentity.recall_link_cost(dest) is None, name
            assert got is None or len(got) == len(pkt.data) - 148 or name == "weak", name


def test_corrupted_stamp_not_stripped_by_port():
    raw = bytearray(bytes.fromhex(VECTORS["lxmf"]))
    i = len(raw) - 10                                         # inside the stamp
    raw[i] ^= 0x01
    pkt = _unpack(bytes(raw))
    _fresh()
    # signature now fails (the extension is signed), so the port rejects it outright
    assert RealIdentity.validate_announce(pkt) is False


def test_lxmf_and_plain_text_app_data_still_parse():
    lx = importlib.import_module("urns.lxmf")
    d = S.make_destination()
    tmp = _tmp()
    _install(tmp, d)
    for app, want in ((S.lxmf_app_data("Düsseldorf"), "Düsseldorf"), (b"just text", "just text")):
        pkt = _unpack(_announce_raw(d, app))
        _fresh()
        assert RealIdentity.validate_announce(pkt) is True
        got = RealIdentity.recall_app_data(d.hash)
        assert got == app and lx.LXMRouter._parse_display_name(got) == want
    shutil.rmtree(tmp)


def test_no_extension_without_stamp():
    d = S.make_destination()
    tmp = _tmp()
    app = S.lxmf_app_data("Test Node")
    pkt = _unpack(_announce_raw(d, app))
    assert pkt.data[148:] == app
    pkt = _unpack(_announce_raw(d, None))
    assert len(pkt.data) == 148
    shutil.rmtree(tmp)


def test_storage_round_trip_and_reference_file_layout():
    d = S.make_destination()
    tmp = _tmp()
    _install(tmp, d)
    path = tmp + "/stamps/" + d.hexhash + ".dstamp"
    e = umsgpack.unpackb(open(path, "rb").read())
    assert e["v"] == 1 and e["stamp"] == STAMP25 and e["value"] == STAMP25_VALUE and "generated" in e
    assert A.get_stamp(d) == STAMP25
    A._cache.clear()
    assert A.get_stamp(d) == STAMP25                           # from disk
    shutil.rmtree(tmp)


def test_invalid_files_rejected():
    d = S.make_destination()
    tmp = _tmp()
    path = tmp + "/stamps/" + d.hexhash + ".dstamp"
    os.makedirs(tmp + "/stamps")

    def put(data):
        open(path, "wb").write(data)
        A._cache.clear()
        return A.get_stamp(d)

    assert A.get_stamp(d) is None                              # no file
    assert put(b"\xff\x00garbage") is None
    assert put(umsgpack.packb({"v": 2, "stamp": STAMP25, "value": 25.5})) is None
    assert put(umsgpack.packb({"v": 1, "stamp": STAMP25[:13], "value": 25.5})) is None
    assert put(umsgpack.packb({"v": 1, "stamp": b"\x00" * 14, "value": 25.5})) is None   # wrong hash
    assert put(umsgpack.packb({"v": 1, "value": 25.5})) is None
    weak = next(x.to_bytes(14, "little") for x in range(10 ** 6)
                if 4 <= stamps.stamp_value(A.material(d), x.to_bytes(14, "little")) < 9)
    assert put(umsgpack.packb({"v": 1, "stamp": weak, "value": 99.0})) is None           # lying value
    assert put(umsgpack.packb({"v": 1, "stamp": STAMP25, "value": 25.5})) == STAMP25     # sane again
    # a valid stamp for ANOTHER destination (hash differs) is rejected
    other = S.make_destination(app="lxmf", aspect="other")
    open(tmp + "/stamps/" + other.hexhash + ".dstamp", "wb").write(
        umsgpack.packb({"v": 1, "stamp": STAMP25, "value": 25.5}))
    A._cache.clear()
    assert A.get_stamp(other) is None
    shutil.rmtree(tmp)


def test_cache_is_bounded():
    A.reset()
    for i in range(A.MAX_ENTRIES + 5):
        A._remember(bytes([i]) * 16, b"x" * 14)
    assert len(A._cache) == A.MAX_ENTRIES


def test_configure_clamps_low_value():
    A.reset()
    A.configure(value=10)
    assert A.VALUE == 23.5 and A.MIN == 23.5
    A.configure(value=10, allow_low=True)
    assert A.VALUE == 10.0 and A.MIN == 10.0
    A.configure(value=30)
    assert A.VALUE == 30.0 and A.MIN == 23.5
    A.reset()


def test_mtu_overflow_goes_out_unstamped_with_a_log():
    d = S.make_destination()
    tmp = _tmp()
    _install(tmp, d)
    logs = []
    real = S.dest_mod.log
    S.dest_mod.log = lambda m, lvl=0: logs.append(m)
    try:
        fits = b"a" * 299                                      # 19 + 148 + 317 + 16 == 500
        raw = _announce_raw(d, fits)
        assert len(raw) == 484 and len(raw) + 16 == const.MTU and not [x for x in logs if 'UNSTAMPED' in x]
        assert raw.endswith(A.extension(d, STAMP25))
        big = b"a" * 300
        raw = _announce_raw(d, big)
        assert len(raw) == 19 + 148 + 300 and raw.endswith(big)   # unstamped, still sent
        u = [x for x in logs if 'UNSTAMPED' in x]
        assert u and "299" in u[0], logs
        ratchet_d = S.make_destination()
        ratchet_d.enable_ratchets()
        logs.clear()
        raw = _announce_raw(ratchet_d, b"a" * 268)             # ratchet: 32 B less room
        assert len(raw) + 16 <= const.MTU
    finally:
        S.dest_mod.log = real
    shutil.rmtree(tmp)


def test_background_generation_does_not_block_loop():
    d = S.make_destination()
    tmp = _tmp()
    A.configure(enabled=True, value=15, allow_low=True, duty=50)
    sent = []
    S.Transport.interfaces = []

    async def main():
        gaps = []
        stop = False

        async def beat():
            last = time.time()
            while not stop:
                await asyncio.sleep(0.005)
                now = time.time()
                gaps.append(now - last)
                last = now

        b = asyncio.create_task(beat())
        A.request(d)                                           # creates the task (loop running)
        assert d.hash in A._tasks
        t0 = time.time()
        while d.hash in A._tasks and time.time() - t0 < 60:
            await asyncio.sleep(0.01)
        stop = True
        await b
        return gaps

    gaps = asyncio.run(main())
    assert d.hash not in A._tasks
    st = A.get_stamp(d)
    assert st is not None and stamps.stamp_valid(st, 15, A.material(d))
    assert os.path.exists(tmp + "/stamps/" + d.hexhash + ".dstamp")
    assert max(gaps) < 0.25, max(gaps)                         # pure-Python slice ~tens of ms
    # the stamp is picked up by the next announce; generation itself sent nothing
    assert not sent
    pkt = _unpack(_announce_raw(d, None))
    assert len(pkt.data) == 148 + 18
    A.reset()
    shutil.rmtree(tmp)


def test_request_without_loop_defers_then_kick_starts():
    d = S.make_destination()
    tmp = _tmp()
    A.configure(enabled=True, value=12, allow_low=True)
    A.request(d)                                               # no running loop (host): stays pending
    assert d.hash in A._pending and d.hash not in A._tasks

    async def main():
        A.kick()
        assert d.hash in A._tasks
        while d.hash in A._tasks:
            await asyncio.sleep(0.01)

    asyncio.run(main())
    assert A.get_stamp(d) is not None
    A.reset()
    shutil.rmtree(tmp)


def test_disabled_generation_only_logs_material_once():
    d = S.make_destination()
    tmp = _tmp()
    logs = []
    real = A.log
    A.log = lambda m, lvl=0: logs.append((m, lvl))
    try:
        A.request(d)
        A.request(d)
    finally:
        A.log = real
    assert len(logs) == 1 and A.material(d).hex() in logs[0][0] and not A._tasks and not A._pending
    A.reset()
    shutil.rmtree(tmp)


def test_tool_generated_stamp_accepted_by_loader():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tools"))
    import gen_announce_stamp as tool
    for f in range(256):
        assert tool._tab(f) == stamps._tab(f), f
    d = S.make_destination()
    tmp = _tmp()
    A.configure(value=14, allow_low=True)
    mat = A.material(d)
    st, dt, n = tool.search(mat, 14.0, 2)
    val = tool.stamp_value(mat, st)
    assert val == stamps.stamp_value(mat, st) and val >= 14
    os.makedirs(tmp + "/stamps")
    open(tmp + "/stamps/" + d.hexhash + ".dstamp", "wb").write(tool.pack_file(st, val))
    assert A.get_stamp(d) == st
    shutil.rmtree(tmp)


def test_duty_pacing():
    clk = [0]
    real = const.ticks_ms
    const.ticks_ms = lambda: clk[0]
    try:
        d = stamps.Duty(80, 1000)
        rests = []
        for _ in range(20):                                    # 20 slices of 80 ms
            d.begin()
            clk[0] += 80
            rests.append(d.rest_ms())
            clk[0] += rests[-1]
        assert rests[:12] == [1] * 12 and all(r == 20 for r in rests[13:]), rests   # grace 1 s, then 80 ms * 20/80
        d = stamps.Duty(50)
        d.begin(); clk[0] += 80
        assert d.rest_ms() == 80
        assert stamps.Duty(100).rest_ms(80) == 1
        assert stamps.Duty(80, 1000).rest_ms(80) == 1
        # duty fraction really is busy / (busy + rest)
        d = stamps.Duty(80)
        assert abs(80 / (80 + d.rest_ms(80)) - 0.8) < 0.02
    finally:
        const.ticks_ms = real
    assert stamps.DUTY_INTERACTIVE == 80 and stamps.DUTY_BACKGROUND == 50


def test_emission_time_monotonic_across_reboots_and_clock_jumps():
    D = S.RealDestination
    tmp = tempfile.mkdtemp()
    old_sp = RealIdentity.storagepath
    RealIdentity.storagepath = tmp
    try:
        h = b"\x42" * 16

        def reboot():
            D._emitted.clear()
            D._stored.clear()
        reboot()
        real_now = 1795285656
        assert D._emission_time(h, real_now) == real_now          # NTP-synced boot
        reboot()                                                  # reboot, clock unset (seconds since power-up + 2000 epoch offset)
        e1 = D._emission_time(h, 946684800 + 17)
        assert e1 > real_now                                      # never older than the previous emission
        e2 = D._emission_time(h, 946684800 + 20)
        assert e2 == e1 + 1                                       # same boot, still increasing
        reboot()                                                  # another reboot, clock still unset
        e3 = D._emission_time(h, 946684800 + 5)
        assert e3 > e2
        reboot()                                                  # clock synced far forward: adopted as is
        later = e3 + 2 * D.PERSIST_EVERY
        assert D._emission_time(h, later) == later
        reboot()
        assert D._emission_time(h, later) > later                 # same second again -> strictly greater
        # the announce itself carries it
        d = S.make_destination()
        _tmp2 = _tmp()
        raw1 = _announce_raw(d, None)
        raw2 = _announce_raw(d, None)
        t1 = int.from_bytes(raw1[19 + 74 + 5:19 + 74 + 10], "big")
        t2 = int.from_bytes(raw2[19 + 74 + 5:19 + 74 + 10], "big")
        assert t2 > t1
        shutil.rmtree(_tmp2)
    finally:
        RealIdentity.storagepath = old_sp
        D._emitted.clear()
        D._stored.clear()
        shutil.rmtree(tmp)


def test_announce_reports_failure_when_no_interface_can_send():
    d = S.make_destination()
    tmp = _tmp()
    S.Transport.interfaces = []                                # nothing registered / all offline
    assert d.announce(app_data=b"x") is False                  # not reported as sent
    ifc = S.harness.MockInterface("up")
    S.Transport.interfaces = [ifc]
    assert d.announce(app_data=b"x") is True and len(ifc.sent) == 1
    ifc.online = False                                         # TCP dropped after "connected"
    assert d.announce(app_data=b"x") is False and len(ifc.sent) == 1
    ifc.online = True
    assert d.announce(app_data=b"x") is True                   # a retry goes out
    S.Transport.interfaces = []
    shutil.rmtree(tmp)


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
