# Host-side tests for TCP dead-connection detection (urns/interfaces/tcp.py).
#
# A TCP client that only listens cannot tell a quiet hub from a dead socket:
# readinto() keeps returning EAGAIN and the interface stays "online" until a
# send happens to fail. Field symptom (reticulum-tdeck #13): announces stop
# after some minutes and resume only when the user sends something. Checks:
# an RX-idle watchdog forces a reconnect, and traffic resets it.
#
# Run:  python3 firmware/tests/test_tcp_dead_link.py

import sys
import os
import asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401  (installs shims + synthetic urns package)

import urns.interfaces.tcp as tcp  # noqa: E402
from urns.interfaces.tcp import TCPClientInterface  # noqa: E402

_failures = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        _failures.append(name)


# Controllable millisecond clock for the watchdog.
_now = [0]
tcp._ticks_ms = lambda: _now[0]
tcp._ticks_diff = lambda a, b: a - b


class FakeSock:
    def __init__(self):
        self.chunks = []           # bytes to hand out; None = EAGAIN
        self.closed = False

    def settimeout(self, t):
        pass

    def connect(self, addr):
        pass

    def setsockopt(self, level, opt, val):
        pass

    def readinto(self, buf):
        if not self.chunks:
            return None
        c = self.chunks.pop(0)
        if c is None:
            return None
        buf[:len(c)] = c
        return len(c)

    def close(self):
        self.closed = True


class FakeSocketMod:
    AF_INET = 2
    SOCK_STREAM = 1
    IPPROTO_TCP = 6

    def __init__(self):
        self.made = []

    def getaddrinfo(self, host, port):
        return [(0, 0, 0, "", (host, port))]

    def socket(self, *a):
        s = FakeSock()
        self.made.append(s)
        return s


def mk(**cfg):
    mod = FakeSocketMod()
    tcp.socket = mod
    c = {"target_host": "hub", "target_port": 4242, "reconnect_wait": 0}
    c.update(cfg)
    iface = TCPClientInterface(c)
    iface.process_incoming = lambda raw: None
    return iface, mod


async def run_ticks(iface, n, step_ms):
    """Drive poll_loop, advancing the fake clock step_ms per scheduler turn."""
    task = asyncio.ensure_future(iface.poll_loop())
    for _ in range(n):
        _now[0] += step_ms
        await asyncio.sleep(0.012)
    iface.enabled = False
    await asyncio.sleep(0.03)
    task.cancel()


def test_silent_socket_reconnects():
    _now[0] = 0
    iface, mod = mk(rx_idle_timeout=10)
    asyncio.run(run_ticks(iface, 60, 500))   # 30s of fake time, no data
    check("reconnected after RX silence", len(mod.made) >= 2, len(mod.made))
    check("old socket closed", mod.made[0].closed)


def test_traffic_resets_watchdog():
    _now[0] = 0
    iface, mod = mk(rx_idle_timeout=10)
    frame = b"\x7e" + bytes([0x00, 0x00]) + b"\x11" * 16 + b"\x00" * 10 + b"\x7e"
    # One frame every 4 fake seconds (8 ticks of 500ms).
    mod.made[0].chunks = ([None] * 7 + [frame]) * 10
    asyncio.run(run_ticks(iface, 70, 500))
    check("no reconnect while data keeps arriving", len(mod.made) == 1,
          len(mod.made))


def test_disabled_watchdog():
    _now[0] = 0
    iface, mod = mk(rx_idle_timeout=0)
    asyncio.run(run_ticks(iface, 60, 500))
    check("rx_idle_timeout=0 never reconnects", len(mod.made) == 1,
          len(mod.made))


for fn in (test_silent_socket_reconnects, test_traffic_resets_watchdog,
           test_disabled_watchdog):
    print(fn.__name__)
    fn()

if _failures:
    print("FAILED:", len(_failures))
    sys.exit(1)
print("all ok")
