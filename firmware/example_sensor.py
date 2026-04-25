"""
µReticulum Example: LXMF Sensor Client with Deepsleep
========================================
Compatible with MeshChat, Sideband, NomadNet over UDP on the same LAN.

Usage (MicroPython on ESP32/Pico W):
  1. Edit config.py — set WiFi credentials, node name, interfaces and sensor 'hub' (LXMF address for data to be sent to)
  2. Copy the urns/ folder, config.py, and this file to the device
  3. Uncomment peripherals you have connected below
  4. Copy example.sensor.py to main.py
  4. Run with: import main

Process
  1. Device boots from sleep
  2. Connects to network (e.g. WiFi)
  3. Announces
  4. Collects data from sensors
  5. Sends data string with send_data()
  6. Waits preset amount of time
  7. Shuts down µReticulum
  8. Goes into deepsleep with a timer to wake

"""

from config import WIFI_SSID, WIFI_PASS, NODE_NAME, DEBUG, CONFIG, SENSOR_HUB

import machine, time
import gc
gc.collect()

# ---- Peripherals ----
# Uncomment the ones you have connected. Shared I2C bus for I2C devices.
#from machine import Pin, SoftI2C
#i2c = SoftI2C(scl=Pin(6), sda=Pin(5), freq=100000)

#import peripherals.bme280_sensor as bme_sensor
#bme_sensor.init(i2c)

# import peripherals.neopixel_led as neopixel_led
# neopixel_led.init(pin=21)

#import peripherals.gpio_control as gpio
#gpio.init({"lamp": (21, "OUT")})

import peripherals.adc_reader as adc_reader
adc_reader.init({"battery": 1, "moisture": 10})

# import peripherals.sds011_sensor as sds011_sensor
# sds011_sensor.init(uart_id=1, tx_pin=43, rx_pin=44)

# List all active peripherals here (must match uncommented imports above)
active_peripherals = [adc_reader]

gc.collect()

# ---- Echo reply ----
ECHO_REPLY = True


async def send_data(router, sensor_hub_addr, content):
    """Send data as async task (crypto takes ~7s, must not block poll loop)."""
    import uasyncio as asyncio

    # Yield so the poll loop can resume immediately
    await asyncio.sleep(0)

    sensor_hub_hash = bytes.fromhex(sensor_hub_addr)

    try:

        msg = router.send_message(sensor_hub_hash, content)
        if msg:
            if DEBUG >= 1:
                print("[SENSOR] Sent to " + sensor_hub_hash.hex()[:8])
        else:
            if DEBUG >= 1:
                print("[SENSOR] Cannot send to " + sensor_hub_hash.hex()[:8] + " (unknown identity)")
    except Exception as e:
        from urns.log import log, LOG_ERROR
        log("[Err] error: " + str(e), LOG_ERROR)
    gc.collect()

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

        # Run content through active peripherals
        results = []
        for p in active_peripherals:
            result = p.process(content)
            if result:
                results.append(result)
        if results:
            content = "\n".join(results)

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
    """Run on MicroPython (ESP32, Pico W, etc.)"""
    import uasyncio as asyncio

    if needs_wifi(CONFIG):
        ip = connect_wifi(WIFI_SSID, WIFI_PASS)
    gc.collect()

    from urns import Reticulum
    from urns.log import LOG_NONE, LOG_NOTICE, LOG_DEBUG

    log_map = {0: LOG_NONE, 1: LOG_NONE, 2: LOG_DEBUG}
    rns = Reticulum(loglevel=log_map.get(DEBUG, LOG_NOTICE))
    rns.config = CONFIG

    new_node_name = "{}".format(NODE_NAME)

    dest, router = setup_node(rns, new_node_name)
    gc.collect()

    rns.setup_interfaces()
    gc.collect()

    # Calling machine.RTC() should set system time from RTC
    # RTC can be set with mpremote rtc --set
    rtc = machine.RTC()
    gc.collect()

    if DEBUG >= 1:
        print("LXMF address:", dest.hexhash)
        print("Free memory:", gc.mem_free(), "bytes")
        print("RTC Time:", rtc.datetime())
        print("Running... (Ctrl+C to stop)")

    # Deferred initial announce — runs AFTER poll loop starts
    async def initial_announce():
        await asyncio.sleep(0.5)
        try:
            router.announce()
            if DEBUG >= 1:
                print("Announced as:", new_node_name)
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

    # Go into deep sleep
    async def start_deepsleep():
        import machine

        await asyncio.sleep(2)

        print("Preparing for deepsleep")
        rns.shutdown()
        print("Shutdown complete")

        machine.deepsleep(60000)
        print("Deepsleep - should not print")

    # Send Data
    async def send_sensor_data():
        await asyncio.sleep(5)
        moisture_sensor_raw = adc_reader.process("moisture")
        battery_sensor_raw = adc_reader.process("battery")

        reply_content = "A1: {},{}".format(battery_sensor_raw, moisture_sensor_raw)

        asyncio.create_task(send_data(router, SENSOR_HUB, reply_content))
        await asyncio.sleep(10)
        await start_deepsleep()

    # Patch rns.run to include our announce + reannounce tasks
    _original_run = rns.run

    async def run_with_reannounce():
        asyncio.create_task(initial_announce())
        asyncio.create_task(reannounce_loop())
        # Start SDS011 periodic measurement if active
        # if sds011_sensor.sensor: sds011_sensor.start()
        asyncio.create_task(send_sensor_data())
        await _original_run()

    try:
        asyncio.run(run_with_reannounce())
    except KeyboardInterrupt:
        rns.shutdown()
        if DEBUG >= 1:
            print("Shutdown complete")


main()
