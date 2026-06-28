"""
µReticulum Example: LXMF Messaging Node
========================================
Compatible with MeshChat, Sideband, NomadNet over UDP on the same LAN.

Usage (MicroPython on ESP32-S3 / RP2040):
  1. Edit config.py — set WiFi credentials, node name, and interfaces
  2. Copy the urns/ folder, config.py, and this file to the device
  3. Uncomment peripherals you have connected below
  4. Run with: import example_node
"""

from config import WIFI_SSID, WIFI_PASS, NODE_NAME, DEBUG, CONFIG

import gc
gc.collect()

# ---- Peripherals ----
# Uncomment the ones you have connected. Shared I2C bus for I2C devices.
# IMPORTANT: I2C pins must NOT overlap with interface pins (E32 uses
# UART1 TX=4, RX=5, AUX=6, M0=15, M1=2). Pick free GPIOs for I2C.
# from machine import Pin, SoftI2C
# i2c = SoftI2C(scl=Pin(21), sda=Pin(20), freq=100000)

# import peripherals.bme280_sensor as bme_sensor
# bme_sensor.init(i2c)

# import peripherals.neopixel_led as neopixel_led
# neopixel_led.init(pin=21)

# import peripherals.gpio_control as gpio
# gpio.init({"lamp": (2, "OUT")})

# Battery sensing is board-declared: activates ONLY if the active board's preset
# in lora_boards.py has a "battery" block (the XIAO ESP32-S3 has none). Then it
# answers a "battery" text command and a "sensor" summary.
import peripherals.adc_reader as adc_reader
from lora_boards import battery_config
_battery = battery_config(CONFIG)
if _battery:
    adc_reader.init({"battery": _battery["pin"]},
                    dividers={"battery": _battery.get("divider", 1.0)})

# import peripherals.sds011_sensor as sds011_sensor
# sds011_sensor.init(uart_id=1, tx_pin=43, rx_pin=44)

# List all active peripherals here (must match uncommented imports above)
active_peripherals = [adc_reader] if _battery else []

gc.collect()

# ---- Echo reply ----
ECHO_REPLY = True


def _peer_name(router, dest_hash):
    """Get display name for a destination hash, or short hex."""
    peer = router.peers.get(dest_hash)
    if peer and peer.get("name"):
        return peer["name"]
    return dest_hash.hex()[:8]


async def _send_msg(router, dest_hash, body):
    """Send LXMF message as async task (crypto is slow)."""
    import uasyncio as asyncio
    await asyncio.sleep(0)
    try:
        msg = router.send_message(dest_hash, body)
        if msg:
            print("[Sent] -> " + dest_hash.hex()[:8])
        else:
            print("[Error] Unknown identity: " + dest_hash.hex()[:8])
    except Exception as e:
        print("[Error] Send failed: " + str(e))
    gc.collect()


async def serial_input_loop(router):
    """Poll stdin for /msg <hash> <body> commands."""
    import sys
    import select
    import uasyncio as asyncio
    from urns.identity import Identity

    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)
    buf = ""

    while True:
        if poller.poll(0):
            ch = sys.stdin.read(1)
            if ch in ("\n", "\r"):
                line = buf.strip()
                buf = ""
                if line.startswith("/msg "):
                    parts = line[5:].split(" ", 1)
                    if len(parts) < 2 or len(parts[0]) < 8:
                        print("Usage: /msg <hex_hash> <message>")
                        continue
                    prefix = parts[0].lower()
                    body = parts[1]
                    # Find destination by hash prefix
                    match = None
                    for dh in Identity.known_destinations:
                        if dh.hex().startswith(prefix):
                            match = dh
                            break
                    if match:
                        asyncio.create_task(_send_msg(router, match, body))
                    else:
                        print("[Error] No known destination: " + prefix)
                elif line:
                    print("Unknown command. Use: /msg <hash> <message>")
            else:
                buf += ch
        await asyncio.sleep(0.05)


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

    # ESP32-S3: deactivate AP interface — dual-interface mode routes broadcast
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

    try:
        import ntptime
        ntptime.settime()
        if DEBUG >= 1:
            print("NTP synced")
    except Exception as e:
        if DEBUG >= 1:
            print("NTP sync failed:", e)

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

        # Run content through active peripherals
        results = []
        for p in active_peripherals:
            result = p.process(content)
            if result:
                results.append(result)
        if results:
            content = "\n".join(results)

        if DEBUG >= 1:
            name = _peer_name(router, message.source_hash)
            print()
            print("<" + name + "/" + sender + "> " + content)

        # Queue async echo reply (non-blocking)
        if ECHO_REPLY:
            asyncio.create_task(send_echo_reply(router, message.source_hash, content))
        gc.collect()

    router.register_delivery_callback(on_message)

    # Announce handler — see other LXMF peers
    def on_announce(destination_hash, display_name):
        if DEBUG >= 1:
            print("[Peer] " + (display_name or "?") + " [" + destination_hash.hex()[:8] + "]")

    router.register_announce_callback(on_announce)

    return dest, router


def needs_wifi(config):
    """Check if any UDP or TCP interface is enabled in config."""
    for iface in config.get("interfaces", []):
        if iface.get("enabled", False) and iface.get("type", "") in (
            "UDPInterface", "TCPClientInterface",
        ):
            return True
    return False


def main():
    """Run on MicroPython (ESP32-S3, RP2040, etc.)"""
    import uasyncio as asyncio

    if needs_wifi(CONFIG):
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
        asyncio.create_task(serial_input_loop(router))
        # Start SDS011 periodic measurement if active
        # if sds011_sensor.sensor: sds011_sensor.start()
        await _original_run()

    try:
        asyncio.run(run_with_reannounce())
    except KeyboardInterrupt:
        rns.shutdown()
        if DEBUG >= 1:
            print("Shutdown complete")


main()
