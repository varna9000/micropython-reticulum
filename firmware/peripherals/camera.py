"""
ESP32-S3 OV2640 Camera Capture — hardware JPEG.

Requires: micropython-camera-API firmware (cnadler86)
  https://github.com/cnadler86/micropython-camera-API/releases

Pin mapping for ESP32-S3-CAM board (from pinout diagram):
  D0=11, D1=9, D2=8, D3=10, D4=12, D5=18, D6=17, D7=16
  XCLK=15, PCLK=13, VSYNC=6, HREF=7, SDA=4, SCL=5

Usage:
  from peripherals.camera import capture

  capture()                                    # VGA q30 (default)
  capture(resolution="qvga", quality=20)       # 320x240
  capture(resolution="cif")                    # 400x296
  capture(grayscale=True)                      # VGA grayscale
  capture(flash="auto", flash_pin=48)          # fill flash (NeoPixel) when dark

JPEG by default. The camera node (example_camera_node.py) re-encodes to WebP
via the webp_fast native module — see docs/CAMERA_WEBP.md.
"""

from camera import Camera, PixelFormat, FrameSize
import gc
import time

DATA_PINS = [11, 9, 8, 10, 12, 18, 17, 16]  # D0-D7

RESOLUTIONS = {
    "96x96":  FrameSize.R96X96,
    "qqvga":  FrameSize.QQVGA,    # 160x120
    "qcif":   FrameSize.QCIF,     # 176x144
    "hqvga":  FrameSize.HQVGA,    # 240x176
    "240x240":FrameSize.R240X240,
    "qvga":   FrameSize.QVGA,     # 320x240
    "cif":    FrameSize.CIF,      # 400x296
    "hvga":   FrameSize.HVGA,     # 480x320
    "vga":    FrameSize.VGA,      # 640x480
    "svga":   FrameSize.SVGA,     # 800x600
    "xga":    FrameSize.XGA,      # 1024x768
    "hd":     FrameSize.HD,       # 1280x720
    "sxga":   FrameSize.SXGA,     # 1280x1024
    "uxga":   FrameSize.UXGA,     # 1600x1200
}

_cam = None


def _flash_set(pin, color):
    """Set the onboard NeoPixel (used as a fill flash). Returns False if unavailable."""
    try:
        import neopixel
        from machine import Pin
        np = neopixel.NeoPixel(Pin(pin), 1)
        np[0] = color
        np.write()
        return True
    except Exception:
        return False


def _measure_brightness(warmup=4):
    """Quick grayscale QQVGA probe -> average luma 0..255 (255 if unavailable).

    A direct, sensor-independent darkness measure (the AEC register doesn't track
    scene brightness reliably on this board). Dark room ~9, lit room ~80-150.
    """
    cam = None
    try:
        cam = Camera(data_pins=DATA_PINS, pclk_pin=13, vsync_pin=6, href_pin=7,
                     sda_pin=4, scl_pin=5, xclk_pin=15, xclk_freq=20000000,
                     pixel_format=PixelFormat.GRAYSCALE, frame_size=FrameSize.QQVGA,
                     fb_count=1)
        for _ in range(warmup):
            cam.capture()
            time.sleep_ms(50)
        buf = cam.capture()
        n = len(buf)
        s = 0
        cnt = 0
        for i in range(0, n, 8):   # sample every 8th pixel — fast, accurate enough
            s += buf[i]
            cnt += 1
        return s // cnt if cnt else 255
    except Exception as e:
        print("[Camera] brightness probe failed:", e)
        return 255   # assume bright (no flash) on failure
    finally:
        if cam is not None:
            try:
                cam.deinit()
            except Exception:
                pass


def capture(path="/photo.jpg", resolution="vga", quality=30, grayscale=False, warmup_frames=8, vflip=True, hmirror=False, exposure=None, ae_level=None, fb_count=1, xclk=20000000, gainceiling=None, flash="off", flash_pin=None, flash_threshold=50, flash_settle=3, flash_color=(255, 150, 210)):
    """Capture a JPEG image.

    Args:
        path: output file path
        resolution: one of "qqvga","qvga","cif","hvga","vga","svga","xga","hd","sxga","uxga"
        quality: JPEG quality 10-63 (lower = smaller file, 10 is fine for LoRa)
        grayscale: if True, capture in grayscale (smaller file, no color)
        warmup_frames: discard this many frames before keeping one so auto
            exposure/gain/white-balance can converge. Too few -> over/under
            exposed. 8-15 is usually enough; raise it if images come out bright.
        vflip: flip image vertically (board-orientation dependent)
        hmirror: mirror image horizontally
        exposure: None = auto-exposure (AEC, the default). An int fixes the
            exposure time: auto-exposure is disabled and the AEC register is set
            to this value (~0..1200, higher = longer/brighter).
        ae_level: when using auto-exposure, bias its target brightness, -2..+2
            (negative = darker). Use this if auto images are over/under exposed.
        fb_count: number of frame buffers. Use 2 for high JPEG quality (q~80+),
            where a single buffer can overflow (cam_hal: FB-OVF) on large frames.
        xclk: camera clock in Hz (default 20 MHz). Lowering it (e.g. 5 MHz, the
            driver's practical floor) lengthens the max exposure for dark scenes.
        gainceiling: None to leave default, or 0..6 (2x..128x) to cap auto-gain
            higher for low light (brighter but noisier).
        flash: "off" (default), "on" (always), or "auto" (fire when dark). Uses the
            onboard NeoPixel on flash_pin as a fill light.
        flash_pin: GPIO of the onboard NeoPixel used as flash (e.g. 48). None = no flash.
        flash_threshold: in "auto", fire when the grayscale brightness probe reads
            below this (0-255). Dark room ~9, lit ~80-150; default 50.
        flash_settle: frames to capture after the flash turns on before keeping one.
        flash_color: NeoPixel (R,G,B) for the flash. WS2812 green is very efficient,
            so plain white (255,255,255) looks green — default (255,150,210) is a
            green-corrected neutral white.
    Returns:
        file size in bytes
    """
    global _cam
    if _cam is not None:
        _cam.deinit()
        _cam = None
        gc.collect()

    fs = RESOLUTIONS.get(resolution.lower())
    if fs is None:
        print("Unknown resolution. Options:", ", ".join(sorted(RESOLUTIONS)))
        return 0

    pf = PixelFormat.GRAYSCALE if grayscale else PixelFormat.JPEG

    # Decide the flash before configuring the real capture — the auto brightness
    # probe needs the sensor to itself. flash: "off" | "on" | "auto".
    fire = (flash == "on")
    if flash == "auto" and flash_pin is not None:
        b = _measure_brightness()
        fire = b < flash_threshold
        print("[Camera] brightness={} thr={} -> flash {}".format(
            b, flash_threshold, "ON" if fire else "off"))

    _cam = Camera(
        data_pins=DATA_PINS,
        pclk_pin=13, vsync_pin=6, href_pin=7,
        sda_pin=4, scl_pin=5, xclk_pin=15,
        xclk_freq=xclk,
        pixel_format=pf,
        frame_size=fs,
        fb_count=fb_count,
    )

    if not grayscale:
        _cam.set_quality(quality)

    # gainceiling 0..6 (2x..128x): raise it in low light so AGC can amplify.
    if gainceiling is not None:
        try:
            _cam.set_gain_ctrl(True)
            _cam.set_gainceiling(int(gainceiling))
        except AttributeError:
            pass

    try:
        _cam.set_vflip(vflip)
        _cam.set_hmirror(hmirror)
    except AttributeError:
        pass  # older camera-API builds without flip/mirror setters

    # Exposure: None keeps auto-exposure (AEC); an int fixes the exposure time.
    # Set before the warmup loop so the discarded frames settle at this value.
    try:
        if exposure is None:
            _cam.set_exposure_ctrl(True)
            if ae_level is not None:
                _cam.set_ae_level(int(ae_level))   # bias auto target darker/brighter
        else:
            _cam.set_exposure_ctrl(False)
            _cam.set_aec_value(int(exposure))
    except AttributeError:
        pass  # older camera-API builds without exposure setters

    # Discard frames so auto exposure/gain/AWB can converge. A short gap lets
    # the sensor integrate and apply new register values between frames.
    for _ in range(warmup_frames):
        _cam.capture()
        time.sleep_ms(50)
    if fire and flash_pin is not None:
        _flash_set(flash_pin, flash_color)          # fill flash on (green-corrected white)
        for _ in range(max(1, flash_settle)):       # let the LED light a full frame
            _cam.capture()
            time.sleep_ms(40)
    img = _cam.capture()
    img_bytes = bytes(img)
    if fire and flash_pin is not None:
        _flash_set(flash_pin, (0, 0, 0))            # flash off
    gc.collect()

    if path:
        with open(path, "wb") as f:
            f.write(img_bytes)
        mode = "grayscale" if grayscale else "q={}".format(quality)
        print("Saved {} ({} bytes, {} {})".format(path, len(img_bytes), resolution, mode))

    return img_bytes
