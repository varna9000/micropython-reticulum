"""
µReticulum Example: LXMF Messaging Node
========================================
Compatible with MeshChat, Sideband, NomadNet over UDP on the same LAN.

Usage (MicroPython on ESP32/Pico W):
  1. Update WIFI_SSID and WIFI_PASS below
  2. Copy the urns/ folder and this file to the device
  3. Run with: import example_node

Usage (Desktop CPython):
  python example_node.py
"""

# ---- Configuration ----
WIFI_SSID = "YourNetworkName"
WIFI_PASS = "YourPassword"
NODE_NAME = "ESP32 Node"
# ------------------------

import gc
gc.collect()


def lxmf_app_data(display_name):
    """Encode display name as LXMF-compatible msgpack: [name_bytes, None]"""
    if isinstance(display_name, str):
        display_name = display_name.encode("utf-8")
    n = len(display_name)
    if n < 256:
        return bytes([0x92, 0xc4, n]) + display_name + bytes([0xc0])
    else:
        return bytes([0x92, 0xc5, n >> 8, n & 0xFF]) + display_name + bytes([0xc0])


def parse_display_name(app_data):
    """Parse display name from LXMF announce app_data"""
    if not app_data or len(app_data) == 0:
        return None
    try:
        first = app_data[0]
        if (0x90 <= first <= 0x9f) or first == 0xdc:
            # msgpack array: [name_bytes, stamp_cost]
            if app_data[1] == 0xc4:
                name_len = app_data[2]
                return app_data[3:3 + name_len].decode("utf-8")
            elif app_data[1] == 0xc0:
                return None
        return app_data.decode("utf-8")
    except Exception:
        return None


def decode_lxmf_message(data, dest_hash):
    """Decode incoming LXMF opportunistic message.
    data: decrypted packet payload (without dest hash)
    dest_hash: our destination hash (16 bytes)
    Returns dict with message info, or None on failure.
    """
    try:
        from urns import umsgpack

        # Reassemble full LXMF frame: dest_hash + data
        lxmf_bytes = dest_hash + data

        # Parse: [dest_hash(16)] [source_hash(16)] [signature(64)] [msgpack_payload]
        DL = 16   # destination length
        SL = 64   # signature length

        source_hash = lxmf_bytes[DL:2 * DL]
        signature = lxmf_bytes[2 * DL:2 * DL + SL]
        packed_payload = lxmf_bytes[2 * DL + SL:]

        payload = umsgpack.unpackb(packed_payload)

        # payload = [timestamp, title, content, fields, ?stamp]
        timestamp = payload[0]
        title = payload[1]
        content = payload[2]
        fields = payload[3] if len(payload) > 3 else {}

        # Try to validate signature
        from urns.identity import Identity
        from urns.crypto.hashes import sha256

        # Strip stamp for hashing if present
        if len(payload) > 4:
            payload = payload[:4]
            packed_payload = umsgpack.packb(payload)

        hashed_part = dest_hash + source_hash + packed_payload
        message_hash = sha256(hashed_part)
        signed_part = hashed_part + message_hash

        sig_valid = False
        source_identity = Identity.recall(source_hash)
        if source_identity:
            try:
                sig_valid = source_identity.validate(signature, signed_part)
            except Exception:
                pass

        # Decode content
        if isinstance(title, bytes):
            try:
                title = title.decode("utf-8")
            except Exception:
                pass
        if isinstance(content, bytes):
            try:
                content = content.decode("utf-8")
            except Exception:
                pass

        return {
            "source": source_hash,
            "content": content,
            "title": title,
            "fields": fields,
            "timestamp": timestamp,
            "hash": message_hash,
            "signature_valid": sig_valid,
        }

    except Exception as e:
        from urns.log import log, LOG_ERROR
        log("LXMF decode error: " + str(e), LOG_ERROR)
        return None


def connect_wifi(ssid, password, timeout=15):
    import network
    import time
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("Connecting to WiFi:", ssid)
        wlan.connect(ssid, password)
        start = time.time()
        while not wlan.isconnected():
            if time.time() - start > timeout:
                raise RuntimeError("WiFi connection timed out")
            time.sleep(0.5)
    ip = wlan.ifconfig()[0]
    print("Connected! IP:", ip)
    return ip


def setup_node(rns, node_name):
    from urns import Destination
    from urns.log import log, LOG_NOTICE

    # Create LXMF delivery destination — visible in MeshChat/Sideband
    dest = Destination(
        rns.identity,
        Destination.IN,
        Destination.SINGLE,
        "lxmf",
        "delivery",
    )

    # Incoming LXMF message handler
    def on_packet(data, packet):
        # Send delivery proof first (so MeshChat knows we received it)
        packet.prove()

        msg = decode_lxmf_message(data, dest.hash)
        if msg:
            verified = "verified" if msg["signature_valid"] else "UNVERIFIED"
            sender = msg["source"].hex()[:8]
            print()
            print("=" * 40)
            print("LXMF Message [" + verified + "]")
            print("  From: " + sender)
            if msg["title"]:
                print("  Title: " + str(msg["title"]))
            print("  Content: " + str(msg["content"]))
            print("=" * 40)
        gc.collect()

    dest.set_packet_callback(on_packet)

    # Announce handler — see other LXMF peers
    def on_announce(destination_hash, app_data, packet):
        name = parse_display_name(app_data) or "?"
        log("Peer: " + name + " [" + destination_hash.hex()[:8] + "]", LOG_NOTICE)

    dest._announce_handler = on_announce

    # Announce ourselves with LXMF-compatible app_data
    app_data = lxmf_app_data(node_name)
    from urns.transport import Transport
    print("Interfaces registered:", len(Transport.interfaces))
    for iface in Transport.interfaces:
        print("  -", iface.name, "online:", iface.online)
    dest.announce(app_data=app_data)

    print("LXMF address:", dest.hexhash)
    print("Announced as:", node_name)
    return dest


def main():
    """Run on MicroPython (ESP32, Pico W, etc.)"""
    import uasyncio as asyncio

    ip = connect_wifi(WIFI_SSID, WIFI_PASS)
    gc.collect()

    from urns import Reticulum
    from urns.log import LOG_DEBUG

    rns = Reticulum(loglevel=LOG_DEBUG)
    rns.setup_interfaces()
    gc.collect()

    dest = setup_node(rns, NODE_NAME)
    gc.collect()
    print("Free memory:", gc.mem_free(), "bytes")

    # Add periodic re-announce to the event loop
    async def reannounce_loop():
        while True:
            await asyncio.sleep(120)
            try:
                app_data = lxmf_app_data(NODE_NAME)
                dest.announce(app_data=app_data)
                print("[Re-announced]")
            except Exception as e:
                print("Re-announce error:", e)
            gc.collect()

    # Patch rns.run to include our reannounce task
    _original_run = rns.run

    async def run_with_reannounce():
        asyncio.create_task(reannounce_loop())
        await _original_run()

    print("Running... (Ctrl+C to stop)")
    try:
        asyncio.run(run_with_reannounce())
    except KeyboardInterrupt:
        rns.shutdown()
        print("Shutdown complete")


def main_desktop():
    """Run on desktop CPython for testing (no WiFi needed)"""
    import asyncio
    import os

    storagedir = "/tmp/urns"
    try:
        os.makedirs(storagedir, exist_ok=True)
    except Exception:
        pass

    from urns import Reticulum
    from urns.log import LOG_DEBUG

    rns = Reticulum(config_path=storagedir + "/config.json", loglevel=LOG_DEBUG)
    rns.setup_interfaces()

    dest = setup_node(rns, "Desktop uRNS Node")

    print("Running...")
    try:
        import threading, time as _time

        def reannounce():
            while True:
                _time.sleep(120)
                try:
                    dest.announce(app_data=lxmf_app_data("Desktop uRNS Node"))
                    print("[Re-announced]")
                except Exception as e:
                    print("Re-announce error:", e)

        threading.Thread(target=reannounce, daemon=True).start()
        asyncio.run(rns.run())
    except KeyboardInterrupt:
        rns.shutdown()


if __name__ == "__main__":
    import sys
    if hasattr(sys.implementation, 'name') and sys.implementation.name == 'micropython':
        main()
    else:
        main_desktop()
