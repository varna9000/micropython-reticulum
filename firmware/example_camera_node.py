"""
µReticulum Example: Camera Node
================================
Responds to incoming LXMF messages with a camera image.
Send any message to this node and it replies with a VGA (640x480) WebP photo
as an LXMF attachment — small enough for LoRa, yet higher resolution than a
same-size JPEG. Falls back to JPEG if the WebP encoder isn't installed.

Resolution, WebP quality, flash, night mode and more are adjustable at runtime
by messaging the node — send 'help' or 'settings'. See docs/CAMERA_WEBP.md.

Usage (MicroPython on ESP32-S3 with OV2640):
  1. Edit config.py — set WiFi credentials, node name, and interfaces
  2. Copy urns/, peripherals/, lib/webp_fast_*.mpy, config.py, and this file
  3. Run with: import example_camera_node
"""

from config import WIFI_SSID, WIFI_PASS, NODE_NAME, DEBUG, CONFIG

import gc
gc.collect()

# Camera settings.
# Default is VGA + WebP: capture a clean q90 JPEG, re-encode to WebP at a very
# low quality. The clean (noise-free) source compresses beautifully, so VGA fits
# the ~16 KB LoRa Resource limit with detail to spare (q1 ~7 KB, q10 ~11 KB).
# Needs /lib/webp_fast_xtensawin.mpy — without it the node falls back to JPEG.
CAM_RESOLUTION = "vga"   # 640x480
CAM_QUALITY = 90         # JPEG capture quality (higher = better/cleaner WebP source)
CAM_FB_COUNT = 2         # frame buffers; 2 needed so a q90 JPEG doesn't overflow (FB-OVF)
CAM_FORMAT = "webp"      # "webp" (default) or "jpg"
CAM_WEBP_QUALITY = 1     # WebP quality 0..100; from a clean source q1 ~7 KB at VGA.
                         # Raise for crisper: q5 ~9 KB, q10 ~11 KB, q20 ~14 KB (all fit).
CAM_WEBP_SCALE = 0       # downscale during decode: 0/1/2/3 = 1/1, 1/2, 1/4, 1/8
CAM_WEBP_METHOD = 4      # WebP effort 0..6 (higher = slower/smaller; VGA m4 ~7 s)
CAM_MAX_BYTES = 15500    # ceiling; WebP quality auto-lowered to stay under the 16 KB limit
CAM_EXPOSURE = None  # None = auto-exposure; int (~0..1200) = fixed exposure time
CAM_AE_LEVEL = 0     # auto-exposure brightness bias -2..+2 (lower if overexposed)
CAM_WARMUP = 12      # frames discarded so auto-exposure can settle
CAM_VFLIP = True     # vertical flip
CAM_HMIRROR = True   # horizontal mirror
CAM_LED_PIN = 48     # onboard NeoPixel (RGB) pin; turned off at boot. None = leave it alone
CAM_FLASH = "off"    # GPIO48 NeoPixel as flash: "off" / "on" / "auto" (when dark).
                     # Onboard LED is weak (close-ups only); "auto" adds ~0.7 s/capture.
CAM_FLASH_THRESHOLD = 50  # "auto" fires when the brightness probe reads below this (0-255)
CAM_FLASH_COLOR = (255, 150, 210)  # green-corrected white (WS2812 green is over-bright)
CAM_NIGHT = False    # night mode: low xclk (long exposure) + high gain + flash for
                     # near-dark scenes. Dim/grainy and slow — best this hardware can do.

# Maps helper keywords -> the module-level setting they change, with help text.
_CAM_SETTINGS = {
    "resolution":   "CAM_RESOLUTION",
    "quality":      "CAM_QUALITY",
    "format":       "CAM_FORMAT",
    "webp_quality": "CAM_WEBP_QUALITY",
    "webp_scale":   "CAM_WEBP_SCALE",
    "webp_method":  "CAM_WEBP_METHOD",
    "flash":        "CAM_FLASH",
    "flash_threshold": "CAM_FLASH_THRESHOLD",
    "night":        "CAM_NIGHT",
    "exposure":     "CAM_EXPOSURE",
    "ae_level":     "CAM_AE_LEVEL",
    "warmup":       "CAM_WARMUP",
    "vflip":        "CAM_VFLIP",
    "hmirror":      "CAM_HMIRROR",
}
_CAM_HELP = {
    "image":        "capture a photo and send it back",
    "settings":     "show the current camera settings",
    "help":         "show this list of keywords",
    "resolution":   "frame size: qqvga, qvga, cif, hvga, vga, svga, xga, ...",
    "quality":      "JPEG capture quality (higher = better/larger)",
    "format":       "image format: webp (smaller) or jpg",
    "webp_quality": "WebP quality 0-100 (auto-lowered to fit the size budget)",
    "webp_scale":   "WebP downscale 0/1/2/3 = 1/1, 1/2, 1/4, 1/8 (speed/size)",
    "webp_method":  "WebP effort 0-6 (higher = slower/smaller)",
    "flash":        "onboard LED flash: off / on / auto (fire when dark)",
    "flash_threshold": "auto-flash fires when brightness probe < this (0-255)",
    "night":        "night mode on/off: long exposure + gain + flash (dim/grainy, slow)",
    "exposure":     "None = auto-exposure, or int ~0..1200 = fixed exposure time",
    "ae_level":     "auto-exposure brightness bias -2..+2 (lower = darker)",
    "warmup":       "frames discarded so auto-exposure can settle",
    "vflip":        "vertical flip on/off",
    "hmirror":      "horizontal mirror on/off",
}


def camera_config(settings=False, help=False, **kwargs):
    """Adjust camera capture settings at runtime, or read the current ones.

    Read:   camera_config(settings=True)        -> {'resolution': 'cif', ...}
    Adjust: camera_config(quality=20, ae_level=-1, warmup=15)
    Help:   camera_config(help=True)            -> keyword list (str)

    Accepted keywords: resolution, quality, exposure, ae_level, warmup.
    Setting calls return the current settings dict (after applying changes).
    The next capture_image() call picks up the new values automatically.
    """
    if help:
        lines = ["Camera keywords (send '<key>' to read, '<key> <value>' to set):"]
        for key in ("image", "settings", "help",
                    "resolution", "quality", "format",
                    "webp_quality", "webp_scale", "webp_method",
                    "flash", "flash_threshold", "night",
                    "exposure", "ae_level", "warmup", "vflip", "hmirror"):
            lines.append("  " + key + " - " + _CAM_HELP[key])
        return "\n".join(lines)

    g = globals()
    for key, value in kwargs.items():
        name = _CAM_SETTINGS.get(key)
        if name is None:
            print("camera_config: unknown setting '" + str(key) + "'")
            continue
        g[name] = value
        if DEBUG >= 1:
            print("[Camera] " + key + " = " + str(value))
    return {key: g[name] for key, name in _CAM_SETTINGS.items()}


def _peer_name(router, dest_hash):
    peer = router.peers.get(dest_hash)
    if peer and peer.get("name"):
        return peer["name"]
    return dest_hash.hex()[:8]


def _led_off():
    """Turn off the onboard NeoPixel so the board LED isn't lit during operation.

    An uninitialised NeoPixel often powers up showing a random colour. Set
    CAM_LED_PIN = None to skip this (or if your board has no NeoPixel on this pin).
    """
    if CAM_LED_PIN is None:
        return
    try:
        import neopixel
        from machine import Pin
        np = neopixel.NeoPixel(Pin(CAM_LED_PIN), 1)
        np[0] = (0, 0, 0)
        np.write()
    except Exception as e:
        if DEBUG >= 2:
            print("[Camera] LED off failed:", e)


def capture_image():
    """Capture a JPEG image and return the bytes."""
    from peripherals.camera import capture
    if CAM_NIGHT:
        # Night mode: drop xclk for a long exposure, max gain, force the flash.
        # Best-effort for near-dark — dim, grainy and slow, but recognizable.
        return capture(path=None, resolution=CAM_RESOLUTION, quality=CAM_QUALITY,
                       vflip=CAM_VFLIP, hmirror=CAM_HMIRROR,
                       warmup_frames=CAM_WARMUP, fb_count=CAM_FB_COUNT,
                       xclk=5000000, exposure=1200, gainceiling=6,
                       flash="on", flash_pin=CAM_LED_PIN, flash_color=CAM_FLASH_COLOR)
    return capture(path=None, resolution=CAM_RESOLUTION, quality=CAM_QUALITY,
                   vflip=CAM_VFLIP, hmirror=CAM_HMIRROR,
                   exposure=CAM_EXPOSURE, ae_level=CAM_AE_LEVEL,
                   warmup_frames=CAM_WARMUP, fb_count=CAM_FB_COUNT,
                   flash=CAM_FLASH, flash_pin=CAM_LED_PIN,
                   flash_threshold=CAM_FLASH_THRESHOLD, flash_color=CAM_FLASH_COLOR)


def _encode_image():
    """Capture a JPEG and return (format_string, bytes) to attach.

    Default path: capture JPEG -> WebP (auto-fit under CAM_MAX_BYTES). Falls back
    to the raw JPEG if WebP is requested-off, the native module is absent, or the
    encode fails. WebP encode is synchronous (~7 s at VGA, m4) — it briefly
    blocks the event loop, so it runs before the Resource transfer starts.
    """
    jpg = capture_image()
    gc.collect()
    if CAM_FORMAT == "webp":
        from peripherals import webp
        if webp.available():
            data = webp.from_jpeg_under(
                jpg, max_bytes=CAM_MAX_BYTES, quality=CAM_WEBP_QUALITY,
                scale=CAM_WEBP_SCALE, method=CAM_WEBP_METHOD)
            gc.collect()
            if data:
                return "webp", data
            if DEBUG >= 1:
                print("[Camera] WebP encode failed; sending JPEG")
        elif DEBUG >= 1:
            print("[Camera] webp_fast.mpy not found; sending JPEG")
    return "jpg", jpg


async def send_image_reply(router, source_hash, content):
    """Capture photo and send it back as LXMF image attachment."""
    import uasyncio as asyncio
    from urns.lxmf import FIELD_IMAGE

    await asyncio.sleep(0)

    try:
        if DEBUG >= 1:
            print("[Camera] Capturing for " + source_hash.hex()[:8] + "...")

        fmt, img_data = _encode_image()
        gc.collect()

        if len(img_data) > CAM_MAX_BYTES and DEBUG >= 1:
            print("[Camera] WARNING image {} B exceeds {} B limit".format(
                len(img_data), CAM_MAX_BYTES))

        if DEBUG >= 1:
            print("[Camera] Sending {} {} bytes...".format(len(img_data), fmt))

        fields = {FIELD_IMAGE: [fmt, img_data]}
        msg = router.send_message(
            source_hash,
            "Camera capture",
            fields=fields,
        )
        if msg:
            if DEBUG >= 1:
                print("[Camera] Sent to " + source_hash.hex()[:8])
        else:
            if DEBUG >= 1:
                print("[Camera] Cannot reply to " + source_hash.hex()[:8] + " (unknown identity)")
    except Exception as e:
        from urns.log import log, LOG_ERROR
        log("Camera reply error: " + str(e), LOG_ERROR)
    gc.collect()


async def send_text_reply(router, source_hash, text):
    """Send a plain-text LXMF reply (used for help/settings/ack responses).

    Uses the router's resilient delivery: reuse an open link if present, else
    opportunistic, else request a path and send once the route is learned.
    """
    import uasyncio as asyncio
    await asyncio.sleep(0)
    try:
        if not router.send_message(source_hash, text):
            if DEBUG >= 1:
                print("[Camera] Cannot reply to " + source_hash.hex()[:8] + " (unknown identity)")
    except Exception as e:
        from urns.log import log, LOG_ERROR
        log("Camera reply error: " + str(e), LOG_ERROR)
    gc.collect()


def _format_settings():
    """Human-readable dump of the current camera settings."""
    s = camera_config(settings=True)
    lines = ["Camera settings:"]
    for key in ("resolution", "quality", "format",
                "webp_quality", "webp_scale", "webp_method",
                "flash", "flash_threshold", "night",
                "exposure", "ae_level", "warmup", "vflip", "hmirror"):
        lines.append("  " + key + " = " + str(s[key]))
    return "\n".join(lines)


def _coerce_setting(key, val):
    """Convert a text value into the right type for a setting."""
    if key in ("resolution", "format", "flash"):
        return val.lower()
    if key in ("vflip", "hmirror", "night"):
        return val.lower() in ("1", "true", "on", "yes")
    if key == "exposure" and val.lower() in ("auto", "none", "off"):
        return None
    # quality, webp_quality, webp_scale, webp_method, ae_level, warmup, exposure
    return int(val)


def _handle_command(text):
    """Parse and apply a setting command. Accepted forms:
        <key>               -> report the current value
        <key> <value>       -> set
        <key>=<value>       -> set
        set <key> <value>   -> set
    Returns the reply string.
    """
    t = text.strip()
    if t.lower().startswith("set "):
        t = t[4:].strip()
    if "=" in t:
        key, _, val = t.partition("=")
        key, val = key.strip().lower(), val.strip()
    else:
        parts = t.split(None, 1)
        key = parts[0].lower() if parts else ""
        val = parts[1].strip() if len(parts) > 1 else None
    if key not in _CAM_SETTINGS:
        # suggest near matches (shared 3-char prefix or substring) — catches typos
        sugg = [k for k in _CAM_SETTINGS
                if (key and (k[:3] == key[:3] or key in k or k in key))]
        hint = (" Did you mean: " + ", ".join(sugg) + "?") if sugg \
               else " Send 'help' for keywords."
        return "Unknown command '" + (key or text) + "'." + hint
    if not val:
        # No value -> report the current setting.
        return key + " = " + str(camera_config(settings=True)[key])
    try:
        value = _coerce_setting(key, val)
    except Exception:
        return "Bad value for " + key + ": " + val
    camera_config(**{key: value})
    return key + " set to " + str(value)


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

    def on_message(message):
        import uasyncio as asyncio

        verified = "verified" if message.signature_validated else "UNVERIFIED"
        sender = message.source_hash.hex()[:8]
        content = message.content_as_string() or ""

        if DEBUG >= 1:
            name = _peer_name(router, message.source_hash)
            print()
            print("<" + name + "/" + sender + "> " + content)

        cmd = content.strip()
        low = cmd.lower()
        src = message.source_hash

        if low == "image":
            asyncio.create_task(send_image_reply(router, src, content))
        elif low == "help":
            asyncio.create_task(send_text_reply(router, src, camera_config(help=True)))
        elif low == "settings":
            asyncio.create_task(send_text_reply(router, src, _format_settings()))
        else:
            # Any other message is treated as a setting command:
            #   "quality" (read), "quality 20" / "quality=20" / "set quality 20"
            asyncio.create_task(send_text_reply(router, src, _handle_command(cmd)))
        gc.collect()

    router.register_delivery_callback(on_message)

    def on_announce(destination_hash, display_name):
        if DEBUG >= 1:
            print("[Peer] " + (display_name or "?") + " [" + destination_hash.hex()[:8] + "]")

    router.register_announce_callback(on_announce)
    return dest, router


def needs_wifi(config):
    for iface in config.get("interfaces", []):
        if iface.get("enabled", False) and iface.get("type", "") in (
            "UDPInterface", "TCPClientInterface",
        ):
            return True
    return False


def main():
    import uasyncio as asyncio

    _led_off()   # turn off the onboard LED at boot

    if needs_wifi(CONFIG):
        ip = connect_wifi(WIFI_SSID, WIFI_PASS)
    gc.collect()

    from urns import Reticulum
    from urns.log import LOG_NONE, LOG_DEBUG

    log_map = {0: LOG_NONE, 1: LOG_NONE, 2: LOG_DEBUG}
    rns = Reticulum(loglevel=log_map.get(DEBUG, LOG_NONE))
    rns.config = CONFIG

    dest, router = setup_node(rns, NODE_NAME)
    gc.collect()

    rns.setup_interfaces()
    gc.collect()

    if DEBUG >= 1:
        print("Camera node ready!")
        print("LXMF address:", dest.hexhash)
        fmt_note = CAM_FORMAT
        if CAM_FORMAT == "webp":
            from peripherals import webp
            fmt_note = "webp" if webp.available() else "webp->JPEG (webp_fast.mpy missing)"
        print("Resolution: {} format: {} quality: {}".format(
            CAM_RESOLUTION, fmt_note, CAM_QUALITY))
        print("Free memory:", gc.mem_free(), "bytes")
        print("Send any message to get a photo back.")

    async def initial_announce():
        await asyncio.sleep(0.5)
        try:
            router.announce()
            if DEBUG >= 1:
                print("Announced as:", NODE_NAME)
        except Exception as e:
            if DEBUG >= 2:
                print("Announce error:", e)
        gc.collect()

    async def reannounce_loop():
        while True:
            await asyncio.sleep(120)
            try:
                router.announce()
            except:
                pass
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
