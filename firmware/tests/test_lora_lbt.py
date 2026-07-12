# Host-side tests for the LoRa interface's CSMA/listen-before-talk logic
# (urns/interfaces/lora.py). Uses the harness shims + a fake SX1262 modem;
# no hardware. LoRaInterface({...}) fails _init_modem under CPython (machine
# shim has no SPI) and comes up offline — the fake modem is injected after.
#
# Run:  python3 firmware/tests/test_lora_lbt.py

import sys
import os
import time as _pytime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401  (installs shims + synthetic urns package)

# MicroPython time API used by the LBT wait loop — shim for CPython.
import time
if not hasattr(time, "ticks_ms"):
    time.ticks_ms = lambda: int(_pytime.monotonic() * 1000)
    time.ticks_add = lambda t, d: t + d
    time.ticks_diff = lambda a, b: a - b
    time.sleep_ms = lambda ms: _pytime.sleep(ms / 1000.0)

from urns.interfaces.lora import LoRaInterface  # noqa: E402


class FakeModem:
    """Scripted SX1262: _cmd(0x15) pops raw RSSI bytes (dBm = -raw/2)."""

    def __init__(self, rssi_seq=(), default_raw=255, fail_probe=False):
        self.rssi_seq = list(rssi_seq)
        self.default_raw = default_raw   # 255 -> -127.5 dBm (quiet)
        self.fail_probe = fail_probe
        self.probes = 0
        self.sent = []
        self.recv_started = 0

    def _cmd(self, fmt, *args, n_read=0, **kw):
        assert args[0] == 0x15 and n_read == 2, "unexpected modem command"
        self.probes += 1
        if self.fail_probe:
            raise OSError("SPI error")
        raw = self.rssi_seq.pop(0) if self.rssi_seq else self.default_raw
        return bytes([0x00, raw])

    def send(self, frame):
        self.sent.append(bytes(frame))

    def start_recv(self, continuous=False):
        self.recv_started += 1


def make_iface(modem=None, **cfg):
    # Fixed numeric threshold by default: these tests exercise the LBT
    # mechanics. The "auto" default (floor calibration) has its own tests.
    base = {"name": "test", "lbt_rssi": -100}
    base.update(cfg)
    iface = LoRaInterface(base)          # offline: no machine.SPI on host
    iface._modem = modem or FakeModem()
    iface.online = True
    return iface


def test_auto_threshold_calibrates_from_floor():
    m = FakeModem(default_raw=196)       # -98 dBm: a noisy board's own floor
    i = make_iface(m, lbt_rssi="auto")
    i._calibrate_lbt()
    assert i._lbt_rssi == -92.0          # floor + 6dB
    # The same floor now probes clear instead of permanently busy
    assert i.process_outgoing(b"x" * 10) is True
    assert i._lbt_waits == 0 and i._lbt_forced == 0


def test_auto_uncalibrated_skips_lbt():
    m = FakeModem(default_raw=100)       # would read busy if LBT were active
    i = make_iface(m, lbt_rssi="auto")   # init failed on host -> never calibrated
    assert i.process_outgoing(b"x" * 10) is True
    assert m.probes == 0


def test_clear_channel_sends_immediately():
    m = FakeModem(default_raw=255)       # -127.5 dBm, quiet
    i = make_iface(m)
    assert i.process_outgoing(b"x" * 100) is True
    assert len(m.sent) == 1
    assert m.probes == 1                 # one probe, no waiting
    assert i._lbt_waits == 0 and i._lbt_forced == 0
    frame = m.sent[0]
    assert len(frame) == 101
    assert frame[0] & 0x01 == 0          # no split flag
    assert frame[0] & 0x0E == 0          # only seq nibble + split bit used
    assert frame[1:] == b"x" * 100
    assert m.recv_started == 1           # back in continuous RX


def test_busy_then_clear_defers():
    # -70 dBm (raw 140) twice, then -110 dBm (raw 220): two slot waits.
    m = FakeModem(rssi_seq=[140, 140, 220])
    i = make_iface(m)                    # default threshold -100 dBm
    t0 = time.ticks_ms()
    assert i.process_outgoing(b"y" * 50) is True
    elapsed = time.ticks_diff(time.ticks_ms(), t0)
    assert len(m.sent) == 1
    assert m.probes == 3
    assert i._lbt_waits == 1 and i._lbt_forced == 0
    assert elapsed >= 25                 # at least two 15ms+ slots


def test_jammed_channel_forces_send_after_cap():
    m = FakeModem(default_raw=140)       # permanently -70 dBm
    i = make_iface(m, lbt_max_ms=120)
    t0 = time.ticks_ms()
    assert i.process_outgoing(b"z" * 10) is True
    elapsed = time.ticks_diff(time.ticks_ms(), t0)
    assert len(m.sent) == 1              # still transmitted
    assert i._lbt_forced == 1
    assert elapsed >= 110                # waited roughly the cap first


def test_threshold_configurable():
    # raw 190 = -95 dBm: busy at default -100, clear at -90.
    m = FakeModem(default_raw=190)
    i = make_iface(m, lbt_rssi=-90)
    assert i.process_outgoing(b"a" * 10) is True
    assert m.probes == 1 and i._lbt_waits == 0


def test_probe_error_reads_clear():
    m = FakeModem(fail_probe=True)
    i = make_iface(m)
    assert i.process_outgoing(b"b" * 10) is True
    assert len(m.sent) == 1              # LBT failure never blocks TX
    assert i._lbt_waits == 0


def test_lbt_disabled_skips_probe():
    m = FakeModem(default_raw=0)         # 0 dBm: would read busy forever
    i = make_iface(m, lbt_rssi=None)
    assert i.process_outgoing(b"c" * 10) is True
    assert m.probes == 0


def test_split_packet_gates_each_frame():
    m = FakeModem()
    i = make_iface(m)
    data = bytes(range(256)) + b"Q" * 44   # 300B -> 2 frames
    assert i.process_outgoing(data) is True
    assert len(m.sent) == 2
    assert m.probes == 2                   # one LBT probe per physical frame
    f1, f2 = m.sent
    assert f1[0] == f2[0]                  # same seq header
    assert f1[0] & 0x01 == 1               # split flag set
    assert len(f1) == 255                  # header + 254B
    assert f1[1:] + f2[1:] == data
    assert m.recv_started == 2


def test_oversize_dropped_without_tx():
    m = FakeModem()
    i = make_iface(m)
    assert i.process_outgoing(b"d" * 600) is False
    assert m.sent == [] and m.probes == 0


def test_offline_returns_false():
    i = make_iface()
    i.online = False
    assert i.process_outgoing(b"e") is False


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
