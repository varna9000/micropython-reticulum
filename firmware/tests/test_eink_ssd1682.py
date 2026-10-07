"""SSD1682 (Heltec E0213A367) e-ink driver — host tests with recorded SPI."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
import eink_fakes as F
F.install()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from peripherals.eink_ssd1682 import SSD1682  # noqa: E402

PINS = dict(sck=3, mosi=2, cs=4, dc=5, rst=6, busy=7, vext=45, vext_active=0, miso=21)
ID_OK = [0, 0, 0, 0, 0, 0, 0, 1]      # 0x01 -> E0213A367
ID_BAD = [0, 0, 0, 0, 0, 0, 1, 0]     # 0x02 -> other panel


def _panel(rotate=90, chip_bits=ID_OK):
    F.reset()
    F.Pin(2).in_bits = list(chip_bits)      # MOSI read back during detect()
    return SSD1682(rotate=rotate, **PINS)


def test_constructor_powers_panel_and_touches_no_spi():
    p = _panel()
    assert ("pin", 45, 0) in F.log, F.log            # VEXT active LOW
    assert F.clock_ms >= 300                         # warm-up waited
    assert not [e for e in F.log if e[0] == "spi"]   # nothing sent yet
    assert p.fb.width == 250 and p.fb.height == 122


def test_detect_reads_id_via_bitbang_and_caches():
    p = _panel()
    assert p.detect() == 0x01
    assert p.detect() == 0x01       # cached: in_bits are exhausted now
    # reset pulse happened before the ID read
    assert ("pin", 6, 0) in F.log and ("pin", 6, 1) in F.log


def test_wrong_panel_id_raises_with_id():
    p = _panel(chip_bits=ID_BAD)
    try:
        p.flush()
    except OSError as e:
        assert "0x02" in str(e), e
    else:
        raise AssertionError("expected OSError")


def test_begin_sends_init_sequence_with_8bit_y_addressing():
    p = _panel()
    p.flush()
    cmds = F.commands(dc=5)
    seq = [(c, d) for c, d in cmds]
    assert seq[0] == (0x12, b""), seq[0]                      # swreset
    assert (0x01, b"\xF9\x00") in seq
    assert (0x3C, b"\x01") in seq
    assert (0x18, b"\x80") in seq
    assert (0x37, b"\x40\x80\x03\x0E") in seq                 # waveform select
    assert (0x11, b"\x03") in seq
    assert (0x44, b"\x00\x0F") in seq
    assert (0x45, b"\x00\xF9") in seq                         # 2 bytes, NOT 4
    assert (0x4E, b"\x00") in seq
    assert (0x4F, b"\x00") in seq                             # 1 byte, NOT 2
    assert (0x10, b"\x01") not in seq and (0x10, b"\xFF") not in seq   # no deep sleep


def _run():
    import traceback
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except Exception as e:
            failed += 1
            print("FAIL  " + name + "  ->  " + repr(e))
            traceback.print_exc()
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
