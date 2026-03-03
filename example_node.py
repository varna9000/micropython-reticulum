"""
µReticulum Example: LXMF Messaging Node
========================================
Compatible with MeshChat, Sideband, NomadNet over UDP on the same LAN.

Usage (MicroPython on ESP32/Pico W):
  1. Edit config.py — set WiFi credentials, node name, and interfaces
  2. Copy the urns/ folder, config.py, and this file to the device
  3. Run with: import example_node
"""

from config import WIFI_SSID, WIFI_PASS, NODE_NAME, DEBUG, CONFIG

import gc
gc.collect()

from machine import Pin, SoftI2C
import neopixel
import sensors.bme280 as bme280

i2c = SoftI2C(scl=Pin(6), sda=Pin(5),freq=100000)
bme = bme280.BME280(i2c=i2c)

#led = neopixel.NeoPixel(Pin(21),1)
colors={"green":(255,0,0),
        "red":(0,255,0),
        "blue":(0,0,255),
        "off":(0,0,0)}

async def send_echo_reply(router, source_hash, content):
    """Send echo reply as async task (crypto takes ~7s, must not block poll loop)."""
    import uasyncio as asyncio

    # Yield so the poll loop can resume immediately
    await asyncio.sleep(0)

    try:
        reply_content = "Echo: " + str(content)
        msg = router.send_message(source_hash, reply_content)
        if msg:
            if DEBUG >= 1:
                print("[Echo] Replied to " + source_hash.hex()[:8])
        else:
            if DEBUG >= 1:
                print("[Echo] Cannot reply to " + source_hash.hex()[:8] + " (unknown identity)")
    except Exception as e:
        from urns.log import log, LOG_ERROR
        log("Echo reply error: " + str(e), LOG_ERROR)
    gc.collect()


def connect_wifi(ssid, password, timeout=15):
    import sys
    import network
    import time

    platform = sys.platform  # "esp32" or "rp2"

    # ESP32: deactivate AP interface — dual-interface mode routes broadcast
    # packets to AP instead of STA, preventing UDP broadcast reception.
    if platform == "esp32":
        ap = network.WLAN(network.AP_IF)
        if ap.active():
            ap.active(False)
            if DEBUG >= 2:
                print("AP_IF deactivated")

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

    # Disable WiFi power management to reliably receive broadcast UDP
    if platform == "esp32":
        wlan.config(pm=0)
    elif platform == "rp2":
        wlan.config(pm=0xa11140)

    ip = wlan.ifconfig()[0]
    if DEBUG >= 1:
        print("Connected! IP:", ip, "(" + platform + ")")
    return ip


def setup_node(rns, node_name):
    from urns.lxmf import LXMRouter
    router = LXMRouter(identity=rns.identity)
    dest = router.register_delivery_identity(rns.identity, display_name=node_name)

    # Incoming LXMF message handler (proof is sent automatically by LXMRouter)
    def on_message(message):
        import uasyncio as asyncio

        verified = "verified" if message.signature_validated else "UNVERIFIED"
        sender = message.source_hash.hex()[:8]
        content = message.content_as_string() or "(binary)"

        if content.lower() in colors.keys():
            #led[0]=colors[content.lower()]
            #led.write()
            pass

        if "sensor" in content.lower():
            t, p, h = bme.values
            content = "Temperature: {}, Pressure: {}, Humidity: {}".format(t, p, h)
            
        if DEBUG >= 1:
            print()
            print("=" * 40)
            print("LXMF Message [" + verified + "]")
            print("  From: " + sender)
            title = message.title_as_string()
            if title:
                print("  Title: " + title)
            print("  Content: " + content)
            print("=" * 40)

        # Queue async echo reply (non-blocking)
        #asyncio.create_task(send_echo_reply(router, message.source_hash, content))
        asyncio.create_task(send_echo_reply(router, message.source_hash, content))
        gc.collect()

    router.register_delivery_callback(on_message)

    # Announce handler — see other LXMF peers
    def on_announce(destination_hash, display_name):
        if DEBUG >= 1:
            print("[Peer] " + (display_name or "?") + " [" + destination_hash.hex()[:8] + "]")

    router.register_announce_callback(on_announce)

    return dest, router


def main():
    """Run on MicroPython (ESP32, Pico W, etc.)"""
    import uasyncio as asyncio

    ip = connect_wifi(WIFI_SSID, WIFI_PASS)
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
        print("Running... (Ctrl+C to stop)")

    # Deferred initial announce — runs AFTER poll loop starts
    async def initial_announce():
        await asyncio.sleep(0.5)
        try:
            router.announce()
            if DEBUG >= 1:
                print("Announced as:", NODE_NAME)
        except Exception as e:
            if DEBUG >= 2:
                print("Initial announce error:", e)
        gc.collect()

    # Add periodic re-announce to the event loop
    async def reannounce_loop():
        while True:
            await asyncio.sleep(120)
            try:
                router.announce()
                if DEBUG >= 2:
                    print("[Re-announced]")
            except Exception as e:
                if DEBUG >= 2:
                    print("Re-announce error:", e)
            gc.collect()

    # Patch rns.run to include our announce + reannounce tasks
    _original_run = rns.run

    async def run_with_reannounce():
        asyncio.create_task(initial_announce())
        asyncio.create_task(reannounce_loop())
        await _original_run()

    try:
        asyncio.run(run_with_reannounce())
    except KeyboardInterrupt:
        rns.shutdown()
        if DEBUG >= 1:
            print("Shutdown complete")


main()

