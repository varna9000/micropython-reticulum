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


def _writes(cmd):
    """data payloads of every `cmd` in the recorded log, in order"""
    return [d for c, d in F.commands(dc=5) if c == cmd]


def _refresh_modes():
    return [d for c, d in F.commands(dc=5) if c == 0x22]


def test_first_flush_is_full_even_if_fast_requested():
    p = _panel()
    p.flush(full=False)
    assert _refresh_modes() == [b"\xF7", b"\x83"], _refresh_modes()
    assert (0x3C, b"\x01") in F.commands(dc=5)


def test_full_flush_writes_both_ram_planes_with_4000_bytes():
    p = _panel()
    p.flush(full=True)
    w24, w26 = _writes(0x24), _writes(0x26)
    assert len(w24) == 1 and len(w24[0]) == 4000, len(w24[0])
    assert len(w26) == 1 and w26[0] == w24[0]


def test_blank_framebuffer_is_all_white_in_ram():
    p = _panel()
    p.flush(full=True)
    assert _writes(0x24)[0] == b"\xff" * 4000


def test_transpose_rotate_90_maps_landscape_pixel_to_portrait_bit():
    p = _panel(rotate=90)
    p.fb.pixel(10, 3, 1)            # landscape (x=10, y=3) black
    p.flush(full=True)
    ram = _writes(0x24)[0]
    # rot 90: portrait column px = xoff + y = 3, portrait row py = 249 - x = 239
    i = 239 * 16 + (3 >> 3)
    bit = 7 - (3 & 7)
    assert ((ram[i] >> bit) & 1) == 0, "black must be a cleared bit"
    assert sum(1 for b in ram if b != 0xFF) == 1, "exactly one byte touched"


def test_transpose_rotate_270_maps_landscape_pixel_to_portrait_bit():
    p = _panel(rotate=270)
    p.fb.pixel(10, 3, 1)
    p.flush(full=True)
    ram = _writes(0x24)[0]
    py = 10
    px = 121 - 3
    i = py * 16 + (px >> 3)
    bit = 7 - (px & 7)
    assert ((ram[i] >> bit) & 1) == 0
    assert sum(1 for b in ram if b != 0xFF) == 1


def test_xoff_shifts_portrait_column():
    F.reset()
    F.Pin(2).in_bits = list(ID_OK)
    p = SSD1682(rotate=90, xoff=6, **PINS)
    p.fb.pixel(0, 0, 1)
    p.flush(full=True)
    ram = _writes(0x24)[0]
    i = 249 * 16                       # rot 90 puts landscape x=0 on the last portrait row
    assert ((ram[i] >> (7 - 6)) & 1) == 0, hex(ram[i])


def test_fast_flush_sequence_and_old_plane_update():
    p = _panel()
    p.flush(full=True)
    F.log.clear()
    p.fb.pixel(0, 0, 1)
    p.flush()                         # second flush -> fast
    cmds = F.commands(dc=5)
    assert (0x3C, b"\x81") in cmds
    order = [c for c, _ in cmds]
    assert order.index(0x24) < order.index(0x22) < order.index(0x26), order
    assert _refresh_modes() == [b"\xFF", b"\x83"]
    assert _writes(0x26)[0] == _writes(0x24)[0]


def test_full_forced_after_ten_fast_and_counter_resets():
    p = _panel()
    p.flush(full=True)
    modes = []
    for _ in range(12):
        F.log.clear()
        p.flush()
        modes.append(_refresh_modes()[0])
    # 10 fast, then a forced full, then fast again
    assert modes[:10] == [b"\xFF"] * 10, modes
    assert modes[10] == b"\xF7", modes
    assert modes[11] == b"\xFF", modes
    F.log.clear()
    p.flush(full=True)
    for _ in range(3):
        F.log.clear()
        p.flush()
        assert _refresh_modes()[0] == b"\xFF"


def test_power_off_after_every_flush():
    p = _panel()
    p.flush(full=True)
    assert _refresh_modes()[-1] == b"\x83"
    F.log.clear()
    p.flush()
    assert _refresh_modes()[-1] == b"\x83"


def test_busy_stuck_high_raises_instead_of_hanging():
    p = _panel()
    F.Pin(7).stuck = 1
    try:
        p.flush()
    except OSError as e:
        assert "busy timeout" in str(e)
    else:
        raise AssertionError("expected OSError")
    assert F.clock_ms >= 10000


def test_clear_fills_white_and_full_refreshes():
    p = _panel()
    p.fb.fill(1)
    p.clear()
    assert _writes(0x24)[-1] == b"\xff" * 4000
    assert _refresh_modes()[0] == b"\xF7"


def test_constructor_power_cycles_panel_with_logic_lines_low():
    """A wedged SSD1682 ignores RST; only a real power cycle clears it, and
    the controller stays parasitically powered through CS/DC/RST unless they
    are held low while VEXT is off (Heltec's customPowerOff does the same)."""
    p = _panel()
    ev = [e for e in F.log if e[0] == "pin" and e[1] in (45, 4, 5, 6)]
    off = ev.index(("pin", 45, 1))                 # VEXT inactive (off)
    on = ev.index(("pin", 45, 0), off)              # then active (on)
    lows_before_off = {e[1] for e in ev[:off] if e[2] == 0}
    assert lows_before_off >= {4, 5, 6}, ev[:off]  # CS, DC, RST low before power off
    highs_after_on = {e[1] for e in ev[on:] if e[2] == 1}
    assert highs_after_on >= {4, 5, 6}, ev[on:]    # released only after power on
    assert F.clock_ms >= 300 + 300                  # off time + warm-up


def test_detect_initialises_hardware_spi_before_bitbanging():
    """ESP32-S3 port quirk seen on hardware: the first SPI init on pins that
    Pin() already drove as GPIO leaves the bus silent, while an SPI that was
    created and released before the bit-bang works. So detect() claims the bus
    once, releases it, bit-bangs, and _begin() creates the real one."""
    p = _panel()
    p.detect()
    kinds = [e[0] for e in F.log]
    first_bitbang_clock = next(i for i, e in enumerate(F.log)
                               if e[0] == "pin" and e[1] == 3 and e[2] == 1)
    assert "spi-init" in kinds[:first_bitbang_clock], kinds[:12]
    assert "spi-deinit" in kinds[:first_bitbang_clock], kinds[:12]
    assert p._spi is None                    # released again before bit-banging


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
