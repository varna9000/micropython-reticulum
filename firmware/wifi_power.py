"""
WiFi power settings, shared by boot.py and the example apps.

Two settings, read from config.py (both optional):

  WIFI_POWER_SAVE  "auto" (default) | True | False
      Modem power saving between beacons. Measured on an ESP32-S3 node on a
      busy TCP hub: chip ~69 C with power saving off, ~55 C with it on, no
      slower replies and no disconnects. It is turned OFF only where it hurts:
      broadcast UDP reception (UDPInterface) is unreliable while the modem
      sleeps. "auto" = on, unless an enabled UDPInterface is configured.

  WIFI_TX_POWER    dBm (default 13) | None
      Transmit power. The ESP32 firmware default is 20 dBm (100 mW), more than a
      node next to its access point needs. None leaves the firmware default.
      Raise it if the node is far from the AP or the link is unreliable.
"""

import sys

DEFAULT_TX_POWER = 13


def _udp_enabled(cfg):
    for iface in (cfg or {}).get("interfaces", ()):
        if iface.get("enabled", True) and iface.get("type", "") == "UDPInterface":
            return True
    return False


def power_save_wanted(config_module=None, cfg=None):
    """True when modem power saving should be on (see WIFI_POWER_SAVE)."""
    setting = getattr(config_module, "WIFI_POWER_SAVE", "auto") if config_module else "auto"
    if setting is True or setting is False:
        return setting
    if cfg is None and config_module is not None:
        cfg = getattr(config_module, "CONFIG", None)
    return not _udp_enabled(cfg)


def apply(wlan, config_module=None, cfg=None, log=None):
    """Apply power saving and TX power to an active STA interface.

    Never raises: a port that lacks one of the settings keeps its default.
    Returns (power_save_on, tx_power_dbm_or_None) as applied."""
    save = power_save_wanted(config_module, cfg)
    WLAN = type(wlan)
    if sys.platform == "rp2":
        off, on = 0xa11140, getattr(WLAN, "PM_PERFORMANCE", 0xa11142)
    else:
        off, on = getattr(WLAN, "PM_NONE", 0), getattr(WLAN, "PM_PERFORMANCE", 1)
    try:
        wlan.config(pm=on if save else off)
    except Exception as e:
        if log:
            log("WiFi power-save setting not applied: " + str(e))
    tx = getattr(config_module, "WIFI_TX_POWER", DEFAULT_TX_POWER) if config_module else DEFAULT_TX_POWER
    if tx is not None:
        try:
            wlan.config(txpower=tx)
        except Exception as e:
            if log:
                log("WiFi TX power not applied: " + str(e))
            tx = None
    if log:
        log("WiFi power save " + ("on" if save else "off")
            + ", TX power " + (str(tx) + " dBm" if tx is not None else "firmware default"))
    return save, tx
