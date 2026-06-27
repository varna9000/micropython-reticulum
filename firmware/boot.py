# boot.py — runs on every power-on / reset, BEFORE the REPL (and any main.py).
#
# By DEFAULT this does nothing: a plain leaf node (LoRa-only, sensor, proxy)
# boots straight to the REPL with no WiFi — no pointless 15 s connect wait.
#
# TRANSPORT NODES: uncomment the execution block at the bottom to bring WiFi +
# WebREPL up automatically at boot, so the bridge's IP side is online early and
# the node is reachable over the network for control (start/stop the router,
# upload fixes, soft-reboot) WITHOUT the USB cable.
#
#   WebREPL: connect a browser WebREPL client (or webrepl_cli.py) to
#            ws://<node-ip>:8266/  and log in with WEBREPL_PASSWORD (config.py).
#
# This never auto-runs the router — the REPL stays free so you start it on demand
# (`import example_transport_router`) and Ctrl-C to stop it. For headless
# auto-start, add a main.py with that import; WebREPL (if enabled below) still
# comes up first, so you can always Ctrl-C in to stop/patch even if the app hangs.
import sys
import time
import gc


def _wifi_up():
    try:
        import config
    except Exception:
        print("boot: no config.py -> skipping WiFi/WebREPL")
        return None
    try:
        import network
        if sys.platform == "esp32":
            ap = network.WLAN(network.AP_IF)
            if ap.active():
                ap.active(False)          # AP_IF off -> broadcast UDP reaches STA
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        if not wlan.isconnected():
            print("boot: WiFi connecting to '%s' ..." % config.WIFI_SSID)
            wlan.connect(config.WIFI_SSID, config.WIFI_PASS)
            t = time.time()
            while not wlan.isconnected():
                if time.time() - t > 15:
                    print("boot: WiFi timeout -> booting without network")
                    return None
                time.sleep(0.5)
        if sys.platform == "esp32":
            wlan.config(pm=0)             # power-save off (reliable broadcast RX)
        ip = wlan.ifconfig()[0]
        print("boot: WiFi up ->", ip)
        return ip
    except Exception as e:
        print("boot: WiFi error:", e)
        return None


# ---------------------------------------------------------------------------
# TRANSPORT NODES ONLY — uncomment the block below to bring WiFi + WebREPL up at
# boot. A transport router needs its IP side online early (and WebREPL is handy
# for headless control). Leaf nodes (LoRa-only, sensor, proxy) do NOT need this
# — leave it commented so the node boots straight to the REPL. See the README
# "Transport mode" section.
# ---------------------------------------------------------------------------
# _ip = _wifi_up()
# if _ip:
#     try:
#         import config
#         import webrepl
#         webrepl.start(password=config.WEBREPL_PASSWORD)
#         print("boot: WebREPL ready -> ws://%s:8266/" % _ip)
#     except Exception as e:
#         print("boot: WebREPL error:", e)

gc.collect()
