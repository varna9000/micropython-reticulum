"""eink_icons: bitmaps, RSSI bars, battery gauge, 2x text, QR blit."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401
import eink_fakes as F
F.install()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import framebuf  # noqa: E402  (the shim)
from peripherals import eink_icons as I  # noqa: E402


def _fb(w=250, h=122):
    return framebuf.FrameBuffer(bytearray(((w + 7) // 8) * h), w, h, framebuf.MONO_HLSB)


def _count(fb, x, y, w, h):
    return sum(fb.pixel(xx, yy) for yy in range(y, y + h) for xx in range(x, x + w))


def test_icon_bitmaps_have_declared_size():
    for name, (w, h, bits) in I.ICONS.items():
        assert w % 8 == 0, name
        assert len(bits) == (w // 8) * h, (name, len(bits))
        assert any(bits), name + " is blank"


def test_icon_draws_only_inside_its_box():
    fb = _fb()
    I.icon(fb, "envelope", 100, 50)
    assert _count(fb, 100, 50, 16, 12) > 20
    assert _count(fb, 0, 0, 250, 122) == _count(fb, 100, 50, 16, 12)


def test_rssi_bars_thresholds():
    assert I.rssi_bars(_fb(), 0, 0, None) == 0
    assert I.rssi_bars(_fb(), 0, 0, -130) == 0
    assert I.rssi_bars(_fb(), 0, 0, -120) == 1
    assert I.rssi_bars(_fb(), 0, 0, -105) == 2
    assert I.rssi_bars(_fb(), 0, 0, -95) == 3
    assert I.rssi_bars(_fb(), 0, 0, -85) == 4
    assert I.rssi_bars(_fb(), 0, 0, -60) == 5


def test_rssi_bars_more_signal_more_ink():
    a, b = _fb(), _fb()
    I.rssi_bars(a, 0, 0, -118)
    I.rssi_bars(b, 0, 0, -70)
    assert _count(b, 0, 0, I.BARS_W, I.BARS_H) > _count(a, 0, 0, I.BARS_W, I.BARS_H)


def test_battery_fill_is_proportional_and_none_is_empty():
    e, h, f = _fb(), _fb(), _fb()
    I.battery(e, 0, 0, None)
    I.battery(h, 0, 0, 50)
    I.battery(f, 0, 0, 100)
    ce, ch, cf = (_count(x, 0, 0, 24, 12) for x in (e, h, f))
    assert ce < ch < cf, (ce, ch, cf)
    assert ce > 30, "outline must still be drawn when percent is None"


def test_text2x_doubles_each_glyph_pixel():
    small, big = _fb(), _fb()
    small.text("A", 0, 0, 1)
    I.text2x(big, "A", 0, 0)
    for y in range(8):
        for x in range(8):
            v = small.pixel(x, y)
            for dy in (0, 1):
                for dx in (0, 1):
                    assert big.pixel(2 * x + dx, 2 * y + dy) == v, (x, y)


def test_qr_code_size_and_quiet_zone_untouched():
    fb = _fb()
    side = I.qr_code(fb, 6, 12, "lxma://" + "ab" * 16 + ":" + "cd" * 64, scale=2)
    assert side == 98
    assert _count(fb, 6, 12, 98, 98) > 1000
    assert _count(fb, 0, 0, 6, 122) == 0           # left margin clean
    assert _count(fb, 104, 0, 146, 122) == 0       # nothing right of the code
    # finder pattern: top-left module is dark -> 2x2 block of ink
    assert fb.pixel(6, 12) == 1 and fb.pixel(7, 13) == 1


from peripherals import eink_status as S  # noqa: E402

STATS = dict(name="Paper", hash="ab12cd34" * 4, uri="lxma://" + "ab12cd34" * 4 + ":" + "ef" * 64,
             rx=123, tx=45, rssi=-87, snr=9.5, freq_khz=868800, sf=8,
             uptime_ms=3_720_000, vbat=3.95, msgs=0)


def test_format_uptime():
    assert S.format_uptime(45_000) == "up 45s"
    assert S.format_uptime(3_720_000) == "up 1h02m"
    assert S.format_uptime(100 * 3600_000) == "up 4d04h"


def test_battery_percent_clamps_and_passes_none():
    assert S.battery_percent(None) is None
    assert S.battery_percent(3.0) == 0
    assert S.battery_percent(4.5) == 100
    assert 45 <= S.battery_percent(3.75) <= 55


def test_render_draws_qr_frame_and_text_column():
    fb = _fb()
    S.render(fb, STATS)
    assert _count(fb, S.QR_X, S.QR_Y, 98, 98) > 1000          # QR present
    assert fb.pixel(0, 0) == 1 and fb.pixel(249, 121) == 1    # 1 px frame
    assert _count(fb, S.COL, 4, 128, 16) > 0                   # 2x headline
    assert _count(fb, S.COL, 40, 100, 8) > 0                   # RX/TX row


def test_render_long_name_truncates_to_eight_chars():
    fb = _fb()
    s = dict(STATS, name="AVeryLongNodeName")
    S.render(fb, s)
    # 8 chars * 16 px = 128 -> nothing may land right of COL+128 on the headline rows
    assert _count(fb, S.COL + 128, 4, 250 - S.COL - 129, 16) == 0


def test_render_tolerates_unknown_rssi_snr_and_battery():
    fb = _fb()
    s = dict(STATS, rssi=None, snr=None, vbat=None)
    S.render(fb, s)                     # must not raise
    assert _count(fb, 0, 0, 250, 122) > 1000


def test_render_shows_envelope_only_with_messages():
    a, b = _fb(), _fb()
    S.render(a, dict(STATS, msgs=0))
    S.render(b, dict(STATS, msgs=3))
    assert _count(b, 232, 36, 16, 12) > _count(a, 232, 36, 16, 12)


def test_qr_framebuffer_prerender_matches_direct_draw():
    """The QR is encoded once at boot (the encoder needs several KB of
    transient heap the running node does not have) and blitted afterwards."""
    uri = "lxma://" + "ab" * 16 + ":" + "cd" * 64
    direct = _fb()
    I.qr_code(direct, 6, 12, uri, 2)
    qfb = I.qr_framebuffer(uri, 2)
    assert qfb.width == 98 and qfb.height == 98
    via = _fb()
    via.blit(qfb, 6, 12)
    assert bytes(via.buf) == bytes(direct.buf)


def test_render_uses_prerendered_qr_without_encoding():
    fb = _fb()
    qfb = I.qr_framebuffer(STATS["uri"], 2)
    s = dict(STATS, uri="x" * 500)          # far too long to encode: must not be touched
    S.render(fb, s, qr_fb=qfb)
    assert _count(fb, S.QR_X, S.QR_Y, 98, 98) > 1000


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
