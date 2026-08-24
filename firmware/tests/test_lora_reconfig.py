# Host-side tests for LoRaInterface.reconfigure(): live-applying new radio
# params (freq/bw/sf/coding_rate/tx_power) without recreating the interface.
# Uses a scripted fake modem; no hardware.
#
# Run:  python3 firmware/tests/test_lora_reconfig.py

import sys
import os
import time as _pytime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401  (installs shims + synthetic urns package)

import time
if not hasattr(time, "ticks_ms"):
    time.ticks_ms = lambda: int(_pytime.monotonic() * 1000)
    time.ticks_add = lambda t, d: t + d
    time.ticks_diff = lambda a, b: a - b
    time.sleep_ms = lambda ms: None

from urns.interfaces.lora import LoRaInterface  # noqa: E402


class FakeModem:
    """Records the reconfigure call sequence. configure() can be told to raise
    to exercise the failure/RX-restore path."""

    def __init__(self, fail_configure=False):
        self.fail_configure = fail_configure
        self.calls = []          # ordered method names
        self.configured = None   # last cfg dict passed to configure()
        self.recv_started = 0

    def standby(self):
        self.calls.append("standby")

    def configure(self, cfg):
        self.calls.append("configure")
        if self.fail_configure:
            raise OSError("SPI error")
        self.configured = dict(cfg)

    def calibrate_image(self):
        self.calls.append("calibrate_image")

    def start_recv(self, continuous=False):
        self.calls.append("start_recv")
        self.recv_started += 1


def make_iface(modem=None):
    iface = LoRaInterface({"name": "test", "freq_khz": 868000, "sf": 7,
                           "bw": "125", "coding_rate": 5, "tx_power": 14,
                           "lbt_rssi": None})
    iface._modem = modem
    iface.online = True
    return iface


_NEW = {"freq_khz": 868800, "bw": "250", "sf": 8, "coding_rate": 6, "tx_power": 22}


def test_reconfigure_maps_params_to_modem_cfg():
    m = FakeModem()
    i = make_iface(m)
    assert i.reconfigure(_NEW) is True
    cfg = m.configured
    assert cfg["freq_khz"] == 868800
    assert cfg["sf"] == 8
    assert cfg["bw"] == "250"
    assert cfg["coding_rate"] == 6
    assert cfg["output_power"] == 22      # tx_power -> output_power


def test_reconfigure_updates_stored_attrs():
    m = FakeModem()
    i = make_iface(m)
    i.reconfigure(_NEW)
    assert i._freq_khz == 868800
    assert i._sf == 8
    assert i._bw == "250"
    assert i._coding_rate == 6
    assert i._tx_power == 22


def test_reconfigure_recomputes_bitrate():
    m = FakeModem()
    i = make_iface(m)
    before = i.bitrate
    i.reconfigure({"freq_khz": 868000, "bw": "125", "sf": 8,
                   "coding_rate": 5, "tx_power": 14})
    # SF7->SF8 at 125k/CR5 halves the on-air bitrate
    assert i.bitrate != before
    assert abs(i.bitrate - 8 * (125000 / 256) * 0.8) < 0.01


def test_reconfigure_standby_before_configure_before_recv():
    m = FakeModem()
    i = make_iface(m)
    i.reconfigure(_NEW)
    assert m.calls.index("standby") < m.calls.index("configure")
    assert m.calls.index("configure") < m.calls.index("start_recv")
    assert m.recv_started == 1             # left back in continuous RX


def test_reconfigure_without_modem_returns_false():
    i = make_iface(None)
    assert i.reconfigure(_NEW) is False
    assert i._sf == 7                      # unchanged


def test_reconfigure_failure_restores_rx_and_keeps_old_params():
    m = FakeModem(fail_configure=True)
    i = make_iface(m)
    assert i.reconfigure(_NEW) is False
    assert i._sf == 7 and i._freq_khz == 868000   # old params untouched
    assert m.recv_started == 1                     # RX restored despite the error


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
