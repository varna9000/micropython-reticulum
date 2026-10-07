"""
µReticulum Example: E-Ink Status Node (Heltec Wireless Paper)
=============================================================
Boots the same LXMF node as example_node.py and paints a read-only status
screen on the 2.13" e-ink: a QR code of this node's lxma:// contact URI,
node name and short hash, RX/TX counters, last RSSI/SNR, radio settings,
uptime and battery. No keyboard, no input.

Usage (MicroPython on the Wireless Paper):
  1. Edit config.py: NODE_NAME, set the LoRa interface's
     "board": "heltec_wireless_paper", check EINK pins.
  2. Copy urns/, peripherals/, lora_boards.py, config.py and this file to the
     device; the `lora` driver package to /lib/lora.
  3. Run with: import example_eink_node

The panel redraws when RX/TX/message counters change or every 30 s, never
more often than every 5 s (a fast refresh blocks the event loop ~0.5 s).
"""
from config import WIFI_SSID, WIFI_PASS, NODE_NAME, DEBUG, CONFIG, EINK

import gc
import time
gc.collect()

# ---- Display first: its buffers must exist before crypto fragments the heap ----
from peripherals.eink_ssd1682 import SSD1682
from peripherals import eink_status
panel = SSD1682(**EINK)
gc.collect()

import peripherals.adc_reader as adc_reader
adc_reader.init_battery(CONFIG)
gc.collect()

REDRAW_PERIOD_MS = 30_000
REDRAW_MIN_GAP_MS = 5_000

_msgs = 0            # LXMF messages received since boot


def connect_wifi(ssid, password, timeout=15):
    import network
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.config(pm=0)
    network.WLAN(network.AP_IF).active(False)
    if not wlan.isconnected():
        wlan.connect(ssid, password)
        t0 = time.time()
        while not wlan.isconnected():
            if time.time() - t0 > timeout:
                raise RuntimeError("WiFi connect timeout")
            time.sleep(0.5)
    return wlan.ifconfig()[0]


def needs_wifi(config):
    for iface in config.get("interfaces", []):
        if iface.get("enabled", False) and iface.get("type", "") in (
            "UDPInterface", "TCPClientInterface",
        ):
            return True
    return False


def setup_node(rns, node_name):
    from urns.lxmf import LXMRouter
    router = LXMRouter(identity=rns.identity)
    dest = router.register_delivery_identity(rns.identity, display_name=node_name)

    def on_message(message):
        global _msgs
        _msgs += 1
        if DEBUG >= 1:
            print("<" + message.source_hash.hex()[:8] + "> "
                  + (message.content_as_string() or "(binary)"))
        gc.collect()

    router.register_delivery_callback(on_message)
    return dest, router


def _lora_config(config):
    for iface in config.get("interfaces", []):
        if iface.get("enabled", False) and iface.get("type") == "LoRaInterface":
            return iface
    return {}


def _lora_iface(rns):
    for iface in rns.interfaces:
        if type(iface).__name__ == "LoRaInterface":
            return iface
    return None


def make_stats_fn(rns, dest, lora_cfg):
    uri = "lxma://" + dest.hexhash + ":" + rns.identity.get_public_key().hex()
    t_boot = time.ticks_ms()

    def stats():
        iface = _lora_iface(rns)
        return {
            "name": NODE_NAME,
            "hash": dest.hexhash,
            "uri": uri,
            "rx": getattr(iface, "rx", 0),
            "tx": getattr(iface, "tx", 0),
            "rssi": getattr(iface, "rssi", None),
            "snr": getattr(iface, "snr", None),
            "freq_khz": lora_cfg.get("freq_khz", 868000),
            "sf": lora_cfg.get("sf", 7),
            "uptime_ms": time.ticks_diff(time.ticks_ms(), t_boot),
            "vbat": adc_reader.battery_voltage(),
            "msgs": _msgs,
        }
    return stats


async def display_loop(get_stats):
    import uasyncio as asyncio
    last_key = None
    last_draw = None
    while True:
        s = get_stats()
        now = time.ticks_ms()
        key = (s["rx"], s["tx"], s["msgs"])
        first = last_draw is None
        gap = None if first else time.ticks_diff(now, last_draw)
        due = first or gap >= REDRAW_PERIOD_MS or (key != last_key and gap >= REDRAW_MIN_GAP_MS)
        if due:
            try:
                eink_status.render(panel.fb, s)
                panel.flush(full=first)
            except OSError as e:
                print("eink:", e)
            last_key = key
            last_draw = now
            gc.collect()
        await asyncio.sleep(1)


def main():
    import uasyncio as asyncio

    if needs_wifi(CONFIG):
        connect_wifi(WIFI_SSID, WIFI_PASS)
    gc.collect()

    from urns import Reticulum
    from urns.log import LOG_NONE, LOG_NOTICE, LOG_DEBUG

    log_map = {0: LOG_NONE, 1: LOG_NONE, 2: LOG_DEBUG}
    rns = Reticulum(loglevel=log_map.get(DEBUG, LOG_NOTICE))
    rns.config = CONFIG

    dest, router = setup_node(rns, NODE_NAME)
    gc.collect()
    rns.setup_interfaces()
    gc.collect()

    if DEBUG >= 1:
        print("LXMF address:", dest.hexhash)
        print("Free memory:", gc.mem_free(), "bytes")

    async def announce_loop():
        await asyncio.sleep(0.5)
        while True:
            try:
                router.announce()
                if DEBUG >= 1:
                    print("Announced as:", NODE_NAME)
            except Exception as e:
                if DEBUG >= 2:
                    print("Announce error:", e)
            gc.collect()
            await asyncio.sleep(120)

    _original_run = rns.run

    async def run_all():
        asyncio.create_task(announce_loop())
        asyncio.create_task(display_loop(make_stats_fn(rns, dest, _lora_config(CONFIG))))
        await _original_run()

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        rns.shutdown()
        if DEBUG >= 1:
            print("Shutdown complete")


main()
