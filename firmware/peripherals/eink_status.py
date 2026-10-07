"""Status screen layout for the 250x122 e-ink node (pure drawing, no I/O).

    +------------------------------------------------------+
    | [QR 98px]  NodeName (2x, max 8 chars)                 |
    |            ab12cd34                      ((antenna))  |
    |            RX 123  TX 45                 [envelope]   |
    |            ▂▄▆█  -87dBm 9.5                           |
    |            868.8MHz SF8                               |
    |            up 1h02m                                   |
    |            [battery]  3.95V 72%                       |
    +------------------------------------------------------+

render(fb, stats) takes a plain dict so the demo app stays thin and this file
is testable on the host. Missing values (None) draw as '--'.
"""
from . import eink_icons as icons

QR_X = 6
QR_Y = 12
COL = 112                     # text column start
_ROW = (4, 24, 40, 56, 72, 88, 104)


def format_uptime(ms):
    s = ms // 1000
    if s < 60:
        return "up %ds" % s
    m = s // 60
    h = m // 60
    d = h // 24
    if d:
        return "up %dd%02dh" % (d, h % 24)
    if h:
        return "up %dh%02dm" % (h, m % 60)
    return "up %dm" % m


def battery_percent(v):
    """LiPo 3.30 V -> 0 %, 4.20 V -> 100 %, linear, clamped. None -> None."""
    if v is None:
        return None
    p = int((v - 3.3) * 100 / 0.9 + 0.5)
    return max(0, min(100, p))


def _num(v, fmt):
    return "--" if v is None else fmt % v


def render(fb, s):
    fb.fill(0)
    fb.rect(0, 0, 250, 122, 1)
    icons.qr_code(fb, QR_X, QR_Y, s["uri"], 2)

    icons.text2x(fb, s["name"][:8], COL, _ROW[0])
    fb.text(s["hash"][:8], COL, _ROW[1], 1)
    icons.icon(fb, "antenna", 232, _ROW[1] - 4)

    fb.text("RX %d TX %d" % (s["rx"], s["tx"]), COL, _ROW[2], 1)
    if s.get("msgs"):
        icons.icon(fb, "envelope", 232, _ROW[2] - 4)

    icons.rssi_bars(fb, COL, _ROW[3] - 3, s["rssi"])
    fb.text(_num(s["rssi"], "%ddBm") + " " + _num(s["snr"], "%.1f"), COL + 28, _ROW[3], 1)

    fb.text("%.1fMHz SF%d" % (s["freq_khz"] / 1000, s["sf"]), COL, _ROW[4], 1)
    fb.text(format_uptime(s["uptime_ms"]), COL, _ROW[5], 1)

    pct = battery_percent(s["vbat"])
    icons.battery(fb, COL, _ROW[6] - 1, pct)
    fb.text(_num(s["vbat"], "%.2fV") + " " + _num(pct, "%d%%"), COL + 28, _ROW[6], 1)
