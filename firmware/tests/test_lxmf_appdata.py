# LXMF announce app_data compression signalling + core field-ID constants
# (host-side).
#
# LXMF 0.9.5 added supported-functionality signalling in announce app_data
# element [2]: a list that may contain SF_COMPRESSION (0x00). A destination
# that lists it tells senders "you may bz2-compress Resources to me". Our
# Resource layer decompresses inbound transfers on every board (native C
# module or the pure-Python fallback in bz2dec), so we advertise
# SF_COMPRESSION and let peers compress large messages to us, saving airtime.
# Element [0] (display name) and [1] (stamp cost) parsing must be unaffected,
# and the app_data stays a valid 3-element msgpack list for older peers that
# only read [0]/[1].
#
# Also pins the LXMF core field-ID constants to their on-wire values so a typo
# can't silently break interop with MeshChat / Sideband / NomadNet.
#
# Run:  python3 firmware/tests/test_lxmf_appdata.py

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
import importlib

lxmf = importlib.import_module("urns.lxmf")
umsgpack = importlib.import_module("urns.umsgpack")
LXMRouter = lxmf.LXMRouter


def test_field_constants_match_wire_values():
    # Pre-existing (must not drift)
    assert lxmf.FIELD_EMBEDDED_LXMS == 0x01
    assert lxmf.FIELD_IMAGE == 0x06
    assert lxmf.FIELD_TICKET == 0x0C
    # Added from upstream LXMF
    assert lxmf.FIELD_EVENT == 0x0D
    assert lxmf.FIELD_RNR_REFS == 0x0E
    assert lxmf.FIELD_RENDERER == 0x0F
    assert lxmf.FIELD_REPLY_TO == 0x30
    assert lxmf.FIELD_REPLY_QUOTE == 0x31
    assert lxmf.FIELD_REACTION == 0x40
    assert lxmf.FIELD_COMMENT == 0x41
    assert lxmf.FIELD_CONTINUATION == 0x42
    assert lxmf.FIELD_CUSTOM_TYPE == 0xFB
    assert lxmf.FIELD_CUSTOM_DATA == 0xFC
    assert lxmf.FIELD_CUSTOM_META == 0xFD
    assert lxmf.FIELD_NON_SPECIFIC == 0xFE
    assert lxmf.FIELD_DEBUG == 0xFF


def test_renderer_and_signalling_constants():
    assert lxmf.RENDERER_PLAIN == 0x00
    assert lxmf.RENDERER_MICRON == 0x01
    assert lxmf.RENDERER_MARKDOWN == 0x02
    assert lxmf.RENDERER_BBCODE == 0x03
    assert lxmf.REACTION_TO == 0x00
    assert lxmf.REACTION_CONTENT == 0x01
    assert lxmf.COMMENT_FOR == 0x00
    assert lxmf.CONTINUATION_OF == 0x00
    assert lxmf.SF_COMPRESSION == 0x00


def test_announce_app_data_advertises_compression():
    r = LXMRouter()
    r.display_name = "node-a"
    data = r._get_announce_app_data()
    peer = umsgpack.unpackb(data)
    assert isinstance(peer, list)
    assert len(peer) == 3
    assert peer[0] == b"node-a"                 # display name
    assert peer[1] is None                      # no stamp cost required
    assert peer[2] == [lxmf.SF_COMPRESSION]     # compression advertised


def test_build_app_data_helper_with_stamp_cost():
    data = LXMRouter._build_app_data("n", stamp_cost=7)
    peer = umsgpack.unpackb(data)
    assert peer == [b"n", 7, [0x00]]


def test_build_app_data_helper_no_name():
    data = LXMRouter._build_app_data(None)
    peer = umsgpack.unpackb(data)
    assert peer == [None, None, [0x00]]


def test_display_name_parsing_unaffected():
    r = LXMRouter()
    r.display_name = "Zoe"
    data = r._get_announce_app_data()
    assert LXMRouter._parse_display_name(data) == "Zoe"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        print("ok " + t.__name__)
        passed += 1
    print("\n%d passed" % passed)
