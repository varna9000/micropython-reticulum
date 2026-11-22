# TCP client reconnect back-off (host-side). Run:
#   python3 firmware/tests/test_tcp_backoff.py
#
# (a) a failure that never reached the hub (WiFi down, DNS failure, connect
#     error) must not count as "remote refusing": after a 10 minute outage the
#     interface is back within a short capped wait, not ~11 minutes later.
# (b) the connection lifetime must survive ticks_diff wrapping (~6.2 days).

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401

import urns.interfaces.tcp as tcp  # noqa: E402
from urns.interfaces.tcp import TCPClientInterface  # noqa: E402

_now = [0]
tcp._ticks_ms = lambda: _now[0]
# wrap-safe like MicroPython's ticks_diff (period 2**30, signed half-range)
tcp._ticks_diff = lambda a, b: ((a - b + (1 << 29)) % (1 << 30)) - (1 << 29)


class _FT:
    t = 1000.0

    @staticmethod
    def time():
        return _FT.t


FakeTime = _FT


class FakeSock:
    def __init__(self, net_up):
        self.net_up = net_up
        self.closed = False

    def settimeout(self, t):
        pass

    def connect(self, addr):
        if not self.net_up[0]:
            raise OSError(113, "EHOSTUNREACH")

    def setsockopt(self, *a):
        pass

    def readinto(self, buf):
        return None

    def close(self):
        self.closed = True


class FakeSocketMod:
    AF_INET = 2
    SOCK_STREAM = 1
    IPPROTO_TCP = 6

    def __init__(self):
        self.net_up = [True]
        self.connects = 0

    def getaddrinfo(self, host, port):
        if not self.net_up[0]:
            raise OSError(-2, "dns")
        return [(0, 0, 0, "", (host, port))]

    def socket(self, *a):
        self.connects += 1
        return FakeSock(self.net_up)


def _mk(**cfg):
    mod = FakeSocketMod()
    tcp.socket = mod
    tcp.time = FakeTime
    FakeTime.t = 1000.0
    _now[0] = 0
    c = {"target_host": "hub", "target_port": 4242, "reconnect_wait": 5}
    c.update(cfg)
    iface = TCPClientInterface(c)
    iface.process_incoming = lambda raw: None
    return iface, mod


def _tick(iface, seconds):
    for _ in range(seconds):
        FakeTime.t += 1
        _now[0] += 1000
        if not iface.online:
            iface._reconnect()


def test_local_outage_does_not_escalate_backoff():
    iface, mod = _mk()
    assert iface.online
    # a healthy hub connection that then drops while WiFi goes away
    _tick(iface, 100)
    mod.net_up[0] = False
    iface.online = False
    _tick(iface, 600)                       # ten minutes without network
    assert not iface.online
    assert iface._delay == 5, "local failures escalated the remote back-off: %r" % iface._delay
    mod.net_up[0] = True
    t0 = FakeTime.t
    for _ in range(300):
        _tick(iface, 1)
        if iface.online:
            break
    assert iface.online, "still offline 5 min after the network came back"
    assert FakeTime.t - t0 <= iface.LOCAL_RETRY_MAX_S + 2, FakeTime.t - t0


def test_hub_closing_quickly_still_escalates():
    iface, mod = _mk()
    up_at = None
    for _ in range(4000):                   # the hub closes every connection after 1 s
        _tick(iface, 1)
        if iface.online:
            if up_at is None:
                up_at = FakeTime.t
            elif FakeTime.t - up_at >= 1:
                iface.online = False
                up_at = None
    assert iface._delay >= 160, iface._delay


def test_week_old_connection_is_healthy_despite_ticks_wrap():
    iface, mod = _mk(rx_idle_timeout=0)
    iface._delay = 40                        # earlier flapping had escalated it
    sock = iface._socket

    async def run():
        task = asyncio.ensure_future(iface.poll_loop())
        for _ in range(8 * 24):              # 8 days in 1 h steps
            _now[0] += 3600 * 1000
            FakeTime.t += 3600
            await asyncio.sleep(0.005)
        iface.enabled = False
        await asyncio.sleep(0.03)
        task.cancel()
    asyncio.run(run())
    assert iface.online and iface._socket is sock
    iface.online = False                     # the hub finally closes it
    iface._reconnect()
    assert iface._delay == 5, "long-lived connection misjudged as short: %r" % iface._delay


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for k, v in tests:
        try:
            v()
            print("PASS", k)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print("FAIL", k, repr(e))
            failed += 1
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
