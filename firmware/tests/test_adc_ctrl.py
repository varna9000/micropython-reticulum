"""adc_reader: optional ADC enable (ctrl) pin, driven only while reading."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness  # noqa: F401  (installs micropython/machine shims)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- fake machine.ADC / machine.Pin -----------------------------------------
_log = []


class _Pin:
    OUT = 1
    IN = 0
    created = {}

    def __init__(self, n, mode=None, pull=None, value=None):
        self.n = n
        self.v = 0 if value is None else value
        _Pin.created[n] = self
        if value is not None:
            _log.append(("pin", n, value))

    def value(self, v=None):
        if v is None:
            return self.v
        self.v = v
        _log.append(("pin", self.n, v))


class _ADC:
    ATTN_0DB = 0
    ATTN_2_5DB = 1
    ATTN_6DB = 2
    ATTN_11DB = 3

    def __init__(self, pin):
        self.pin = pin

    def atten(self, a):
        pass

    def read_u16(self):
        _log.append(("read", self.pin.n))
        return 32768          # 1.65 V at the pin


machine = sys.modules["machine"]
machine.Pin = _Pin
machine.ADC = _ADC

import peripherals.adc_reader as adc_reader  # noqa: E402


def _reset():
    adc_reader.channels.clear()
    adc_reader._ctrl.clear()
    _log.clear()
    _Pin.created.clear()


def test_no_ctrl_pin_behaves_as_before():
    _reset()
    adc_reader.init({"battery": 20}, dividers={"battery": 2.0})
    v = adc_reader.read_voltage("battery")
    assert abs(v - 3.3) < 0.01, v
    assert not any(e[0] == "pin" for e in _log), _log


def test_ctrl_pin_driven_active_during_read_then_released():
    _reset()
    adc_reader.init({"battery": 20}, dividers={"battery": 2.0},
                    ctrl={"battery": (19, 0)})
    # construction parks the pin at the INACTIVE level (1 for active-low)
    assert ("pin", 19, 1) in _log, _log
    _log.clear()
    adc_reader.read_voltage("battery")
    kinds = [e for e in _log if e[0] in ("pin", "read")]
    assert kinds[0] == ("pin", 19, 0), kinds          # enabled first
    assert all(e[0] == "read" for e in kinds[1:-1]), kinds
    assert kinds[-1] == ("pin", 19, 1), kinds         # released last


def test_init_battery_reads_ctrl_from_board_block():
    _reset()
    config = {"interfaces": [{"board": "heltec_wireless_paper"}],
              "lora_boards": {"heltec_wireless_paper": {
                  "battery": {"pin": 20, "divider": 2.0,
                              "ctrl_pin": 19, "ctrl_active": 0}}}}
    bat = adc_reader.init_battery(config)
    assert bat["ctrl_pin"] == 19
    gate_pin, active = adc_reader._ctrl["battery"]     # (Pin object, level)
    assert gate_pin.n == 19 and active == 0


def test_preset_exists_with_radio_pins():
    from lora_boards import LORA_BOARDS
    p = LORA_BOARDS["heltec_wireless_paper"]
    assert (p["sck_pin"], p["mosi_pin"], p["miso_pin"], p["cs_pin"],
            p["busy_pin"], p["dio1_pin"], p["reset_pin"]) == (9, 10, 11, 8, 13, 14, 12)
    assert p["dio2_rf_sw"] is True and p["dio3_tcxo_millivolts"] == 1800
    assert p["battery"]["pin"] == 20 and p["battery"]["ctrl_pin"] == 19
    assert p["battery"]["ctrl_active"] == 0


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
