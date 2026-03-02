"""
µReticulum Example: NomadNet Page-Serving Node
================================================
Serves simple micron-format pages to NomadNet clients over Reticulum Links.
Pages are loaded from the pages/ directory as .mu (micron format) files.

Supported template variables in .mu files:
  {node_name}  — node display name from config
  {mem_free}   — free heap memory in bytes
  {uptime}     — system uptime in seconds (epoch)

Usage (MicroPython on ESP32/Pico W):
  1. Edit config.py — set WiFi credentials and interfaces
  2. Copy the urns/ folder, config.py, pages/, and this file to the device
  3. Run with: import example_nomadnet_node
"""

from config import WIFI_SSID, WIFI_PASS, NODE_NAME, DEBUG, CONFIG

import gc
import time
gc.collect()
_boot_time = time.time()


def connect_wifi(ssid, password, timeout=15):
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
        print("Connected! IP:", ip)
    return ip


def load_pages(dest, pages_dir="pages"):
    """Load .mu pages from a directory and register them as request handlers.

    Each .mu file becomes a NomadNet page at /page/<filename>.
    Template variables {node_name}, {mem_free}, {uptime} are substituted
    at serve time.
    """
    import os

    try:
        files = os.listdir(pages_dir)
    except OSError:
        if DEBUG >= 1:
            print("Pages directory not found:", pages_dir)
        return 0

    count = 0
    for fname in files:
        if not fname.endswith(".mu"):
            continue

        filepath = pages_dir + "/" + fname
        page_path = "/page/" + fname

        def fmt_mem(b):
            if b >= 1048576:
                return "%.1f MB" % (b / 1048576)
            elif b >= 1024:
                return "%.1f KB" % (b / 1024)
            return str(b) + " B"

        def fmt_uptime(s):
            d, s = divmod(int(s), 86400)
            h, s = divmod(s, 3600)
            m, s = divmod(s, 60)
            parts = []
            if d: parts.append(str(d) + "d")
            if h: parts.append(str(h) + "h")
            if m: parts.append(str(m) + "m")
            parts.append(str(s) + "s")
            return " ".join(parts)

        def make_handler(fpath):
            def handler(path, data, request_id, link_id, remote_identity, requested_at):
                try:
                    with open(fpath, "rb") as f:
                        page = f.read()
                except OSError:
                    return b">Page Not Found\n"
                page = page.replace(b"{node_name}", NODE_NAME.encode("utf-8"))
                try:
                    page = page.replace(b"{mem_free}", fmt_mem(gc.mem_free()).encode("utf-8"))
                except:
                    page = page.replace(b"{mem_free}", b"?")
                page = page.replace(b"{uptime}", fmt_uptime(time.time() - _boot_time).encode("utf-8"))
                return page
            return handler

        dest.register_request_handler(
            page_path,
            response_generator=make_handler(filepath),
            allow=dest.ALLOW_ALL,
        )
        count += 1
        if DEBUG >= 2:
            print("Loaded page:", page_path, "from", filepath)

    if DEBUG >= 1:
        print("Loaded", count, "page(s) from", pages_dir + "/")
    return count


def main():
    import uasyncio as asyncio

    ip = connect_wifi(WIFI_SSID, WIFI_PASS)
    gc.collect()

    from urns import Reticulum, Destination
    from urns.log import LOG_NONE, LOG_NOTICE, LOG_DEBUG

    log_map = {0: LOG_NONE, 1: LOG_NOTICE, 2: LOG_DEBUG}
    rns = Reticulum(loglevel=log_map.get(DEBUG, LOG_NOTICE))
    rns.config = CONFIG
    gc.collect()

    # Create NomadNet node destination: nomadnetwork.node
    dest = Destination(
        rns.identity, Destination.IN, Destination.SINGLE,
        "nomadnetwork", "node",
    )
    dest.set_default_app_data(NODE_NAME.encode("utf-8"))
    dest.accepts_links(True)

    # Load pages from pages/ directory
    load_pages(dest)

    def on_link(link):
        if DEBUG >= 1:
            print("[Link] Established:", link)

    dest.set_link_established_callback(on_link)
    gc.collect()

    rns.setup_interfaces()
    gc.collect()

    if DEBUG >= 1:
        print("NomadNet node address:", dest.hexhash)
        print("Free memory:", gc.mem_free(), "bytes")
        print("Running... (Ctrl+C to stop)")

    async def initial_announce():
        await asyncio.sleep(0.5)
        try:
            dest.announce()
            if DEBUG >= 1:
                print("Announced as:", NODE_NAME)
        except Exception as e:
            if DEBUG >= 2:
                print("Announce error:", e)
        gc.collect()

    async def reannounce_loop():
        while True:
            await asyncio.sleep(300)
            try:
                dest.announce()
                if DEBUG >= 2:
                    print("[Re-announced]")
            except Exception as e:
                if DEBUG >= 2:
                    print("Re-announce error:", e)
            gc.collect()

    _original_run = rns.run

    async def run_with_announce():
        asyncio.create_task(initial_announce())
        asyncio.create_task(reannounce_loop())
        await _original_run()

    try:
        asyncio.run(run_with_announce())
    except KeyboardInterrupt:
        rns.shutdown()
        if DEBUG >= 1:
            print("Shutdown complete")


main()
