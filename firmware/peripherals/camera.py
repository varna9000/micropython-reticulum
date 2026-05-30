"""
ESP32-S3 OV2640 Camera Capture — hardware JPEG.

Requires: micropython-camera-API firmware (cnadler86)
  https://github.com/cnadler86/micropython-camera-API/releases

Pin mapping for ESP32-S3-CAM board (from pinout diagram):
  D0=11, D1=9, D2=8, D3=10, D4=12, D5=18, D6=17, D7=16
  XCLK=15, PCLK=13, VSYNC=6, HREF=7, SDA=4, SCL=5

Usage:
  from cam_capture import capture

  capture()                                    # VGA q30 (default)
  capture(resolution="qvga", quality=20)       # 320x240
  capture(resolution="hvga", quality=15)       # 480x320
  capture(resolution="cif")                    # 400x296
  capture(grayscale=True)                      # VGA grayscale
"""

from camera import Camera, PixelFormat, FrameSize
import gc

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


def capture(path="/photo.jpg", resolution="vga", quality=30, grayscale=False, warmup_frames=3, vflip=True, hmirror=False):
    """Capture a JPEG image.

    Args:
        path: output file path
        resolution: one of "qqvga","qvga","cif","hvga","vga","svga","xga","hd","sxga","uxga"
        quality: JPEG quality 10-63 (lower = smaller file, 10 is fine for LoRa)
        grayscale: if True, capture in grayscale (smaller file, no color)
        warmup_frames: discard this many frames before keeping one (AGC/AWB settle)
        vflip: flip image vertically (board-orientation dependent)
        hmirror: mirror image horizontally
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

    _cam = Camera(
        data_pins=DATA_PINS,
        pclk_pin=13, vsync_pin=6, href_pin=7,
        sda_pin=4, scl_pin=5, xclk_pin=15,
        xclk_freq=20000000,
        pixel_format=pf,
        frame_size=fs,
        fb_count=1,
    )

    if not grayscale:
        _cam.set_quality(quality)

    try:
        _cam.set_vflip(vflip)
        _cam.set_hmirror(hmirror)
    except AttributeError:
        pass  # older camera-API builds without flip/mirror setters

    for _ in range(warmup_frames):
        _cam.capture()  # discard for AGC/AWB settling
    img = _cam.capture()
    img_bytes = bytes(img)
    gc.collect()

    if path:
        with open(path, "wb") as f:
            f.write(img_bytes)
        mode = "grayscale" if grayscale else "q={}".format(quality)
        print("Saved {} ({} bytes, {} {})".format(path, len(img_bytes), resolution, mode))

    return img_bytes
