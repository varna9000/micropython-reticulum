"""
µReticulum Example: Transport Router  (LoRa <-> WiFi bridge)
============================================================
Turns an ESP32-S3 that has BOTH a LoRa radio and WiFi into a Reticulum
**transport node** that relays packets between the two interfaces in both
directions — extending a LoRa mesh onto your IP LAN (MeshChat / Sideband /
NomadNet / rnsd) and carrying IP-side traffic back out over LoRa.

This is a pure relay — it does not host an LXMF inbox. With `enable_transport`
on, the Transport engine automatically:
  * propagates announces across both interfaces (stamping itself as the relay),
  * forwards opportunistic messages and returns their delivery proofs,
  * carries Link sessions (MeshChat/Sideband) and Resource transfers through,
  * clamps link MTU at the LoRa/WiFi boundary,
  * answers path requests from its cache, and
  * persists its path table to flash so a reboot isn't a mesh blackout.

A node behind the relay reaches a peer on the far side because it learns the
relay's transport id from the relay's re-broadcast announces, then addresses
traffic "via" the relay; the relay looks up its path table and forwards on the
one correct interface (not a flood).

Hardware (verified on real hardware):
  Seeed XIAO ESP32-S3 + Wio-SX1262 ("Meshtastic kit"), MicroPython 1.28, ~8 MB
  free heap (PSRAM). 868.8 MHz / SF8 / BW125 / TX22 / syncword 0x1424.

Prerequisites:
  mpremote mip install lora-sx126x lora-sync
  (Recommended) the native ed25519 .mpy module — without it, Ed25519 announce
  validation is pure-Python and slow (seconds per *new* destination; the
  announce fast-path skips re-validation for already-known peers).

Setup:
  1. Set WIFI_SSID / WIFI_PASS / NODE_NAME below.
  2. Make sure every node on the LoRa mesh shares the same radio params
     (freq_khz, sf, bw, coding_rate, syncword) — they're in config.py's CONFIG.
  3. Point the TCP interface's target_host/target_port at your RNS TCP server
     (a public transport node, or rnsd on your LAN with a TCPServerInterface).
  4. Copy urns/, lora_boards.py, and this file to the board.
  5. Run with:  import example_transport_router
"""

import gc
gc.collect()

# ---- Node settings ----
# ALL node config lives in config.py — this example is just a runner. config.py
# provides WIFI_SSID/WIFI_PASS, WEBREPL_PASSWORD, and CONFIG (interfaces +
# transport settings, incl. the TCP target). This file is the relay role;
# example_node.py reads the SAME CONFIG to run as an LXMF inbox instead.
from config import WIFI_SSID, WIFI_PASS, CONFIG
NODE_NAME = "uRNS-Router"

# 0 = silent, 1 = relay activity (announces, paths), 2 = full forwarding trace
DEBUG = 1

# ---- Live HTTP monitor (plain HTTP on the LAN, NOT Reticulum) ----
# Dashboard at http://<node-ip>:<port>/ to watch tables / relay counters / log live.
WEB_MONITOR = True
WEB_PORT = 80


def connect_wifi(ssid, password, timeout=15):
    """Bring up STA WiFi (and NTP). Mirrors example_node.connect_wifi: AP_IF off
    and power-management disabled so broadcast UDP is received reliably."""
    import sys
    import network
    import time

    platform = sys.platform
    if platform == "esp32":
        ap = network.WLAN(network.AP_IF)
        if ap.active():
            ap.active(False)

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        if DEBUG >= 1:
            print("Connecting to WiFi:", ssid)
        wlan.connect(ssid, password)
        start = time.time()
        while not wlan.isconnected():
            if time.time() - start > timeout:
                raise RuntimeError("WiFi connection timed out")
            time.sleep(0.5)

    if platform == "esp32":
        wlan.config(pm=0)
    elif platform == "rp2":
        wlan.config(pm=0xa11140)

    ip = wlan.ifconfig()[0]
    if DEBUG >= 1:
        print("WiFi connected:", ip)

    try:
        import ntptime
        ntptime.settime()
        if DEBUG >= 1:
            print("NTP synced")
    except Exception as e:
        if DEBUG >= 1:
            print("NTP sync failed:", e)
    return ip


def main():
    import uasyncio as asyncio

    # WiFi is required for the bridge (the IP side of the relay).
    ip = connect_wifi(WIFI_SSID, WIFI_PASS)
    gc.collect()

    from urns import Reticulum, Transport
    from urns.log import LOG_NONE, LOG_NOTICE, LOG_DEBUG

    log_map = {0: LOG_NONE, 1: LOG_NOTICE, 2: LOG_DEBUG}
    rns = Reticulum(loglevel=log_map.get(DEBUG, LOG_NOTICE))
    rns.config = CONFIG

    rns.setup_interfaces()
    gc.collect()

    # Battery gauge (board-declared in lora_boards.py; self-disables if the board
    # has no divider). The dashboard reads it live via adc_reader.battery_voltage.
    import peripherals.adc_reader as adc_reader
    adc_reader.init_battery(CONFIG)

    online = [str(i) for i in Transport.interfaces if i.online]
    print("=" * 56)
    print(" µReticulum Transport Router:", NODE_NAME)
    print(" Relay transport id:", rns.identity.hash.hex())
    print(" Transport mode    :", Transport.transport_enabled)
    print(" Online interfaces :", online)
    print(" Free memory       :", gc.mem_free(), "bytes")
    print(" Persistence       :", "on -> " + str(Transport.persist_path)
          if Transport.persist_path else "off")
    _vbat = adc_reader.battery_voltage()
    if _vbat is not None:
        print(" Battery           : %.2f V" % _vbat)
    if WEB_MONITOR:
        print(" Web monitor       : http://" + ip + ":" + str(WEB_PORT) + "/")
    print(" Bridging LoRa <-> WiFi. Ctrl+C to stop.")
    print("=" * 56)

    # Periodic one-line view of the routing tables, so you can watch it work.
    async def status_loop():
        while True:
            await asyncio.sleep(30)
            line = ("[router] paths=%d reachable=%d links=%d reverse=%d cache=%d "
                    "queued=%d | RELAYED ann=%d data=%d link=%d proof=%d | free=%d" % (
                        len(Transport.path_table),
                        len(Transport.reachable_destinations),
                        len(Transport.link_table),
                        len(Transport.reverse_table),
                        len(Transport.packet_cache),
                        len(Transport.announce_table),
                        Transport.relayed_announces, Transport.relayed_data,
                        Transport.relayed_links, Transport.relayed_proofs,
                        gc.mem_free()))
            _vbat = adc_reader.battery_voltage()
            if _vbat is not None:
                line += " | batt=%.2fV" % _vbat
            print(line)
            gc.collect()

    # Run the Transport event loop (job_loop + per-interface poll loops) plus
    # our status reporter.
    _original_run = rns.run

    async def run_with_status():
        asyncio.create_task(status_loop())
        if WEB_MONITOR:
            try:
                import webmonitor
                asyncio.create_task(webmonitor.serve(
                    node_name=NODE_NAME, port=WEB_PORT,
                    battery_fn=adc_reader.battery_voltage))
            except Exception as e:
                print("Web monitor failed:", e)
        await _original_run()

    try:
        asyncio.run(run_with_status())
    except KeyboardInterrupt:
        rns.shutdown()          # also persists the path table
        print("Router shutdown complete")


main()
