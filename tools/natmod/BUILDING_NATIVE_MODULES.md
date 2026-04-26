# Building Native C Modules

µReticulum includes optional native C modules that provide significant speedups over the pure Python implementations. If the `.mpy` files are missing, the firmware falls back to pure Python transparently.

## Prerequisites

1. **MicroPython source tree** — clone and check out the version matching your firmware:
   ```bash
   git clone https://github.com/micropython/micropython.git
   cd micropython && git checkout v1.25.0  # match your firmware version
   ```

2. **Cross-compiler toolchains:**
   - **ESP32/ESP32-S3 (Xtensa):** Install [ESP-IDF](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/get-started/) which includes `xtensa-esp-elf-gcc`
   - **RP2040 (ARM):** Install `arm-none-eabi-gcc` via your package manager:
     ```bash
     # macOS
     brew install arm-none-eabi-gcc
     # Ubuntu/Debian
     sudo apt install gcc-arm-none-eabi
     ```

3. **Update `MPY_DIR`** in each module's `Makefile` to point to your MicroPython source tree.

## Available Modules

### ed25519_fast — Ed25519/X25519 Crypto (160x speedup)

Wraps [Monocypher](https://monocypher.org/) for Ed25519 signing/verification and X25519 key exchange.

```bash
cd tools/natmod/ed25519_fast

# ESP32-S3 (Xtensa)
export PATH="/path/to/xtensa-esp-elf/bin:$PATH"
make clean && make ARCH=xtensawin
cp ed25519_fast.mpy ../../../firmware/lib/ed25519_fast_xtensawin.mpy

# RP2040 (ARM Cortex-M0+)
make clean && make ARCH=armv6m CROSS=arm-none-eabi- \
  CFLAGS_EXTRA="-Iinclude -ffreestanding -nostdinc -isystem $(arm-none-eabi-gcc -print-file-name=include)"
cp ed25519_fast.mpy ../../../firmware/lib/ed25519_fast_armv6m.mpy
```

Upload to device:
```bash
mpremote cp firmware/lib/ed25519_fast_xtensawin.mpy :lib/ed25519_fast_xtensawin.mpy
```

### bz2_fast — BZ2 Compression & Decompression

Pure C bz2 compressor and decompressor for Resource transfers. Produces stdlib-compatible bz2 output (verified against Python's `bz2.decompress()`). Without this module, decompression falls back to pure Python and compression is skipped (Resources sent uncompressed).

```bash
cd tools/natmod/bz2_fast

# ESP32-S3 (Xtensa)
export PATH="/path/to/xtensa-esp-elf/bin:$PATH"
make clean && make ARCH=xtensawin
cp bz2_fast.mpy ../../../firmware/lib/bz2_fast_xtensawin.mpy

# RP2040 (ARM Cortex-M0+)
make clean && make ARCH=armv6m CROSS=arm-none-eabi- \
  CFLAGS_EXTRA="-Iinclude -ffreestanding -nostdinc -isystem $(arm-none-eabi-gcc -print-file-name=include)"
cp bz2_fast.mpy ../../../firmware/lib/bz2_fast_armv6m.mpy
```

Upload to device:
```bash
mpremote cp firmware/lib/bz2_fast_xtensawin.mpy :lib/bz2_fast_xtensawin.mpy
```

## Installation Path

Native `.mpy` modules must be placed in the `/lib/` directory on the device. MicroPython automatically searches `/lib/` for imports. The project structure mirrors this: `firmware/lib/` contains all pre-built `.mpy` files, and `mpremote cp -r firmware/ :` uploads them to the correct location.

## .mpy Format Version

The pre-built `.mpy` files use format version 6 (MicroPython 1.19+). If you're using a newer MicroPython that changes the format, rebuild from source using the steps above.

## Architecture Reference

| ARCH | Cross-compiler | Devices |
|------|---------------|---------|
| `xtensawin` | `xtensa-esp-elf-` (via ESP-IDF) | ESP32, ESP32-S2, ESP32-S3 |
| `armv6m` | `arm-none-eabi-` | RP2040 (Raspberry Pi Pico W) |

## Troubleshooting

- **`xtensa-esp32-elf-gcc: Command not found`** — Add the ESP-IDF toolchain to PATH: `export PATH="/path/to/.espressif/tools/xtensa-esp-elf/.../bin:$PATH"`
- **`assert.h: No such file or directory`** (ARM) — Add `-Iinclude -ffreestanding -nostdinc` to `CFLAGS_EXTRA`
- **`.mpy import fails on device** — Architecture mismatch. Rebuild with the correct `ARCH` for your MCU.
