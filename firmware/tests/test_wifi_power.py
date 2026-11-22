"""wifi_power: WiFi power saving / TX power settings from config.py."""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wifi_power  # noqa: E402


class FakeWLAN:
    PM_NONE = 0
    PM_PERFORMANCE = 1
    PM_POWERSAVE = 2

    def __init__(self, refuse=()):
        self.set = {}
        self.refuse = refuse

    def config(self, **kw):
        for k, v in kw.items():
            if k in self.refuse:
                raise ValueError("unknown config param")
            self.set[k] = v


def _cfg(**attrs):
    m = types.ModuleType("config")
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


UDP = {"interfaces": [{"type": "UDPInterface", "enabled": True}]}
TCP = {"interfaces": [{"type": "TCPClientInterface", "enabled": True},
                      {"type": "UDPInterface", "enabled": False}]}


def test_auto_turns_power_saving_on_without_udp():
    w = FakeWLAN()
    save, tx = wifi_power.apply(w, _cfg(CONFIG=TCP))
    assert save is True and w.set["pm"] == FakeWLAN.PM_PERFORMANCE
    assert tx == wifi_power.DEFAULT_TX_POWER and w.set["txpower"] == 13


def test_auto_keeps_power_saving_off_with_an_enabled_udp_interface():
    w = FakeWLAN()
    save, _ = wifi_power.apply(w, _cfg(CONFIG=UDP))
    assert save is False and w.set["pm"] == FakeWLAN.PM_NONE


def test_explicit_settings_win_over_auto():
    w = FakeWLAN()
    assert wifi_power.apply(w, _cfg(CONFIG=UDP, WIFI_POWER_SAVE=True))[0] is True
    assert w.set["pm"] == FakeWLAN.PM_PERFORMANCE
    w = FakeWLAN()
    assert wifi_power.apply(w, _cfg(CONFIG=TCP, WIFI_POWER_SAVE=False))[0] is False
    assert w.set["pm"] == FakeWLAN.PM_NONE


def test_tx_power_configurable_and_none_leaves_firmware_default():
    w = FakeWLAN()
    assert wifi_power.apply(w, _cfg(CONFIG=TCP, WIFI_TX_POWER=8.5))[1] == 8.5
    assert w.set["txpower"] == 8.5
    w = FakeWLAN()
    assert wifi_power.apply(w, _cfg(CONFIG=TCP, WIFI_TX_POWER=None))[1] is None
    assert "txpower" not in w.set


def test_no_config_module_uses_defaults():
    w = FakeWLAN()
    assert wifi_power.apply(w, None) == (True, wifi_power.DEFAULT_TX_POWER)


def test_unsupported_settings_never_raise():
    logged = []
    w = FakeWLAN(refuse=("pm", "txpower"))
    save, tx = wifi_power.apply(w, _cfg(CONFIG=TCP), log=logged.append)
    assert save is True and tx is None and len(logged) == 3


def test_rp2_uses_cyw43_power_modes():
    real = sys.platform
    try:
        wifi_power.sys.platform = "rp2"

        class RP2WLAN(FakeWLAN):
            PM_PERFORMANCE = 0xa11142
        w = RP2WLAN()
        wifi_power.apply(w, _cfg(CONFIG=UDP))
        assert w.set["pm"] == 0xa11140
        w = RP2WLAN()
        wifi_power.apply(w, _cfg(CONFIG=TCP))
        assert w.set["pm"] == 0xa11142
    finally:
        wifi_power.sys.platform = real


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok " + t.__name__)
    print("\n%d/%d passed" % (len(tests), len(tests)))
