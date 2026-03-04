"""
T-Deck v1 LXMF Messaging Node
===============================
Standalone LoRa messaging device using LilyGO T-Deck v1.
ESP32-S3 + SX1262 LoRa + ST7789 display + keyboard + trackball.

Usage:
  1. Upload urns/ to /lib, t-deck files to root or /lib
  2. import tdeck_node
"""

import gc
gc.threshold(4096)
gc.collect()

from machine import Pin, SPI, SoftI2C
import time

from tdeck_config import (
    NODE_NAME, DEBUG, CONFIG, LORA_CONFIG,
    DISP_CS, DISP_DC, DISP_BL,
    LORA_CS, LORA_MISO,
    KBD_SCL, KBD_SDA, KBD_PWR, KBD_ADDR,
)

# --- Peripheral power ON ---
pwr = Pin(KBD_PWR, Pin.OUT)
pwr.on()
time.sleep_ms(100)

# --- Shared SPI bus (display + LoRa) ---
# Start at 2MHz for LoRa compatibility; display functions will reconfigure
spi = SPI(1, baudrate=8_000_000, sck=Pin(40), mosi=Pin(41), miso=Pin(LORA_MISO))

# Display CS — we manage this to keep it deasserted during LoRa ops.
# LoRa CS is managed internally by the lora-sx126x driver.
_disp_cs = Pin(DISP_CS, Pin.OUT, value=1)


def spi_acquire_display():
    """Acquire SPI bus for display: ensure display CS can be driven."""
    pass  # Display driver manages its own CS via st7789


def spi_release_display():
    """Release SPI bus from display: deassert display CS."""
    _disp_cs.value(1)


def spi_acquire_lora():
    """Acquire SPI bus for LoRa: deassert display CS."""
    _disp_cs.value(1)


def spi_release_lora():
    """Release SPI bus from LoRa."""
    pass  # LoRa driver manages its own CS


# --- Init display ---
import st7789py as st7789
import vga2_8x16 as font

dc = Pin(DISP_DC, Pin.OUT)
bl = Pin(DISP_BL, Pin.OUT)
bl.value(1)

spi_acquire_display()
tft = st7789.ST7789(spi, 240, 320, dc=dc, cs=_disp_cs, backlight=bl, rotation=1)
tft.fill(st7789.BLACK)
tft.text(font, "Starting...", 100, 112, st7789.WHITE, st7789.BLACK)
spi_release_display()

gc.collect()

# --- Init keyboard ---
i2c = SoftI2C(scl=Pin(KBD_SCL), sda=Pin(KBD_SDA), freq=400000, timeout=50000)
time.sleep_ms(500)

# Drain startup garbage
for _ in range(20):
    try:
        i2c.readfrom(KBD_ADDR, 1)
    except OSError:
        pass
    time.sleep_ms(20)


def get_key():
    try:
        return i2c.readfrom(KBD_ADDR, 1)
    except OSError:
        return b'\x00'


gc.collect()

# --- Init sound ---
from sound import Sound
sound = Sound()
try:
    sound.init()
except Exception as e:
    if DEBUG >= 1:
        print("Sound init failed:", e)
    sound.enabled = False

gc.collect()

# --- Init Reticulum ---
from urns import Reticulum
from urns.lxmf import LXMRouter
from urns.log import LOG_NONE, LOG_NOTICE, LOG_DEBUG

log_map = {0: LOG_NONE, 1: LOG_NONE, 2: LOG_DEBUG}
rns = Reticulum(loglevel=log_map.get(DEBUG, LOG_NOTICE))

# Inject shared SPI + bus arbitration into LoRa config
LORA_CONFIG["spi"] = spi
LORA_CONFIG["spi_acquire"] = spi_acquire_lora
LORA_CONFIG["spi_release"] = spi_release_lora

rns.config = CONFIG
gc.collect()

# --- Setup LXMF ---
router = LXMRouter(identity=rns.identity)
dest = router.register_delivery_identity(rns.identity, display_name=NODE_NAME)
gc.collect()

# --- Init GUI ---
from ui import UI

gui = UI(tft, font, get_key, node_name=NODE_NAME)
gc.collect()

# --- Setup interfaces (LoRa comes online) ---
spi_acquire_lora()
rns.setup_interfaces()
spi_release_lora()
gc.collect()

if DEBUG >= 1:
    print("LXMF address:", dest.hexhash)
    print("Free memory:", gc.mem_free(), "bytes")


# --- Callbacks ---

# Maps LXMF delivery hash -> GUI peer key (populated by on_announce)
_lxmf_to_peer = {}


def _compute_lxmf_hash(dest_hash):
    """Compute the LXMF delivery hash for the identity behind dest_hash."""
    from urns.identity import Identity
    from urns.destination import Destination
    data = Identity.known_destinations.get(dest_hash)
    if data and data[2]:
        id_hash = Identity.truncated_hash(data[2])
        return Destination.hash(id_hash, "lxmf", "delivery")
    return None


def on_message(message):
    """Incoming LXMF message handler."""
    content = message.content_as_string() or "(binary)"
    source_hash = message.source_hash

    # Map LXMF source_hash to GUI peer key via precomputed mapping
    peer_key = _lxmf_to_peer.get(source_hash)

    if peer_key is None:
        # Fallback: compute LXMF hash for source and check mapping
        # (handles case where source IS the LXMF delivery hash)
        peer_key = source_hash

        # Try identity-based lookup: find any peer with same public key
        from urns.identity import Identity
        src_data = Identity.known_destinations.get(source_hash)
        if src_data and src_data[2]:
            src_pk = src_data[2]
            for pk in gui._peer_keys:
                if pk == source_hash:
                    continue
                pk_data = Identity.known_destinations.get(pk)
                if pk_data and pk_data[2] == src_pk:
                    peer_key = pk
                    # Cache for future lookups
                    _lxmf_to_peer[source_hash] = pk
                    break

    if DEBUG >= 1:
        print("[RX] from", source_hash.hex()[:8], "-> peer", peer_key.hex()[:8])

    # Ensure peer exists in GUI
    if peer_key not in gui.peers:
        gui.add_peer(peer_key, source_hash.hex()[:8])

    # Add to chat under the GUI peer key
    gui.add_chat_message(peer_key, False, content)

    # Update RSSI from interface
    for iface in rns.interfaces:
        if iface.rssi is not None:
            gui.rssi = iface.rssi
            break

    # Play notification
    sound.play_rx()
    gc.collect()


def on_announce(destination_hash, display_name):
    """Peer announce handler. Builds LXMF hash mapping and deduplicates peers."""
    # Compute the LXMF delivery hash for this identity
    lxmf_hash = _compute_lxmf_hash(destination_hash)

    # Deduplicate: if another peer key already maps to the same LXMF hash,
    # update that peer instead of adding a duplicate.
    if lxmf_hash:
        existing = _lxmf_to_peer.get(lxmf_hash)
        if existing and existing != destination_hash and existing in gui.peers:
            # Same node, different destination — update existing peer
            gui.peers[existing]["name"] = display_name or gui.peers[existing].get("name", "?")
            _lxmf_to_peer[lxmf_hash] = existing
            gui.dirty = True
            if DEBUG >= 1:
                print("[Peer] (dedup)", display_name or "?",
                      "[" + destination_hash.hex()[:8] + " -> " + existing.hex()[:8] + "]")
            gc.collect()
            return
        # Store mapping: LXMF hash -> this GUI peer key
        _lxmf_to_peer[lxmf_hash] = destination_hash

    rssi = None
    for iface in rns.interfaces:
        if iface.rssi is not None:
            rssi = iface.rssi
            break
    gui.add_peer(destination_hash, display_name, rssi=rssi)
    if DEBUG >= 1:
        print("[Peer]", display_name or "?", "[" + destination_hash.hex()[:8] + "]")
    gc.collect()


router.register_delivery_callback(on_message)
router.register_announce_callback(on_announce)


# --- GUI -> LXMF wiring ---

def gui_send(dest_hash, text):
    """Called by GUI when user sends a message."""
    import uasyncio as asyncio
    asyncio.create_task(_async_send(dest_hash, text))


async def _async_send(dest_hash, text):
    """Send LXMF message as async task (crypto is slow)."""
    import uasyncio as asyncio
    await asyncio.sleep(0)

    try:
        msg = router.send_message(dest_hash, text)
        if msg:
            sound.play_tx()
            if DEBUG >= 1:
                print("[TX] Sent to", dest_hash.hex()[:8])
        else:
            gui.add_chat_message(dest_hash, True, "(send failed: unknown peer)")
            gui.dirty = True
            if DEBUG >= 1:
                print("[TX] Failed: unknown identity for", dest_hash.hex()[:8])
    except Exception as e:
        gui.add_chat_message(dest_hash, True, "(send error)")
        gui.dirty = True
        if DEBUG >= 1:
            print("[TX] Error:", e)
    gc.collect()


def gui_announce():
    """Called by GUI when user presses 'a'."""
    try:
        router.announce()
        if DEBUG >= 1:
            print("[Announced as", NODE_NAME + "]")
    except Exception as e:
        if DEBUG >= 1:
            print("Announce error:", e)
    gc.collect()


gui.on_send = gui_send
gui.on_announce = gui_announce


# --- Async tasks ---

async def initial_announce():
    import uasyncio as asyncio
    await asyncio.sleep(0.5)
    try:
        router.announce()
        if DEBUG >= 1:
            print("Announced as:", NODE_NAME)
    except Exception as e:
        if DEBUG >= 2:
            print("Initial announce error:", e)
    gc.collect()


async def reannounce_loop():
    import uasyncio as asyncio
    while True:
        await asyncio.sleep(120)
        try:
            router.announce()
            gui.announce_flash = time.time()
            gui.dirty = True
            if DEBUG >= 2:
                print("[Re-announced]")
        except Exception as e:
            if DEBUG >= 2:
                print("Re-announce error:", e)
        gc.collect()


# --- Main ---

def main():
    import uasyncio as asyncio

    gc.threshold(-1)  # Relax GC for runtime

    if DEBUG >= 1:
        print("Starting event loop...")

    _original_run = rns.run

    async def run_all():
        asyncio.create_task(initial_announce())
        asyncio.create_task(reannounce_loop())
        asyncio.create_task(gui.kbd_loop())
        asyncio.create_task(gui.gui_loop(spi_acquire_display, spi_release_display))
        asyncio.create_task(gui.battery_loop(spi_acquire_display, spi_release_display))
        await _original_run()

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        sound.deinit()
        rns.shutdown()
        if DEBUG >= 1:
            print("Shutdown complete")


main()
