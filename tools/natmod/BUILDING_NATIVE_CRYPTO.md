# Building the Native Crypto Module

This guide explains how to cross-compile the `ed25519_fast` native module (`.mpy`) from source. You only need this if:

- A new MicroPython version changes the `.mpy` format (currently version 6, stable since MicroPython 1.19)
- You need to target a new architecture
- You want to modify the C code

The pre-built `.mpy` files in `firmware/urns/crypto/` work with MicroPython 1.19+ and don't need recompilation unless the above applies.

## Prerequisites

### 1. Cross-compiler

**ESP32/ESP32-S3 (xtensawin):**
Install ESP-IDF which includes the Xtensa toolchain, or install standalone:
```bash
# The compiler is typically at:
# ~/.espressif/tools/xtensa-esp-elf/*/xtensa-esp-elf/bin/xtensa-esp32s3-elf-gcc
```

**RP2040 (armv6m):**
```bash
# macOS
brew install arm-none-eabi-gcc

# Ubuntu/Debian
sudo apt install gcc-arm-none-eabi
```

### 2. MicroPython source tree

Only needed for headers and build tools — you don't need to build MicroPython itself.

```bash
git clone --depth 1 https://github.com/micropython/micropython.git ~/micropython
cd ~/micropython/mpy-cross && make
```

### 3. Python packages

```bash
pip install pyelftools ar
```

## Build

```bash
cd natmod/ed25519_fast
```

**ESP32 / ESP32-S3:**
```bash
export PATH="$HOME/.espressif/tools/xtensa-esp-elf/esp-14.2.0_20260121/xtensa-esp-elf/bin:$PATH"
make clean && make ARCH=xtensawin
```

**RP2040:**
```bash
make clean && make ARCH=armv6m CROSS=arm-none-eabi- CFLAGS_EXTRA="-Iinclude -ffreestanding -nostdinc -isystem $(arm-none-eabi-gcc -print-file-name=include)"
```

The output is `ed25519_fast.mpy` in the build directory.

## Install

Rename with the architecture suffix and copy to the device:

```bash
# ESP32-S3
cp ed25519_fast.mpy firmware/urns/crypto/ed25519_fast_xtensawin.mpy
mpremote cp firmware/urns/crypto/ed25519_fast_xtensawin.mpy :urns/crypto/ed25519_fast_xtensawin.mpy

# RP2040
cp ed25519_fast.mpy firmware/urns/crypto/ed25519_fast_armv6m.mpy
mpremote cp firmware/urns/crypto/ed25519_fast_armv6m.mpy :urns/crypto/ed25519_fast_armv6m.mpy
```

The Python wrapper (`firmware/urns/crypto/ed25519.py`) auto-detects the platform and loads the correct `.mpy` file. If the file is missing or fails to load, it falls back to the pure Python implementation.

## Verify

On the device REPL:

```python
import ed25519_fast_xtensawin  # or ed25519_fast_armv6m
import os, time

seed = os.urandom(32)
msg = b"test"

t = time.ticks_ms()
sig = ed25519_fast_xtensawin.sign(msg, seed)
print("sign:", time.ticks_diff(time.ticks_ms(), t), "ms")

pk = ed25519_fast_xtensawin.publickey(seed)
t = time.ticks_ms()
ok = ed25519_fast_xtensawin.verify(sig, msg, pk)
print("verify:", time.ticks_diff(time.ticks_ms(), t), "ms, ok:", ok)

t = time.ticks_ms()
pub = ed25519_fast_xtensawin.x25519_publickey(os.urandom(32))
print("x25519:", time.ticks_diff(time.ticks_ms(), t), "ms")
```

Expected: ~12ms sign, ~18ms verify, ~13ms x25519 on ESP32-S3 @ 240MHz.

## Architecture Reference

| `ARCH` | `CROSS` | Devices |
|--------|---------|---------|
| `xtensawin` | `xtensa-esp32s3-elf-` | ESP32, ESP32-S2, ESP32-S3 |
| `armv6m` | `arm-none-eabi-` | RP2040 (Pico, Pico W) |
| `armv7m` | `arm-none-eabi-` | STM32F4/F7 (Pyboard) |
| `x64` | (host gcc) | Linux/macOS x86_64 (testing) |

## Source Files

```
natmod/ed25519_fast/
├── main.c                  # MicroPython bindings (dynruntime.h API)
├── monocypher.c            # Monocypher 4.0.2 core (X25519, BLAKE2b EdDSA)
├── monocypher.h            # Monocypher core header
├── monocypher-ed25519.c    # Standard Ed25519 with SHA-512 (RFC 8032)
├── monocypher-ed25519.h    # Ed25519 header
├── libc_stubs.c            # memcpy/memset/memcmp for natmod (no libc)
├── compat.h                # Inline overrides (optional, for debugging)
├── Makefile                # Build configuration
└── include/                # Stub headers for freestanding builds (ARM)
    ├── assert.h
    ├── string.h
    ├── stdlib.h
    ├── stddef.h
    ├── errno.h
    ├── stdio.h
    └── limits.h
```

## API

The native module exposes these functions:

```python
import ed25519_fast_xtensawin as ef

# Ed25519
sig = ef.sign(message_bytes, seed_32_bytes)        # -> bytes(64)
ok  = ef.verify(sig_64, message_bytes, pk_32)      # -> bool
pk  = ef.publickey(seed_32_bytes)                   # -> bytes(32)

# X25519
shared = ef.x25519(private_32, public_32)           # -> bytes(32)
pk     = ef.x25519_publickey(private_32)             # -> bytes(32)
```

**Important:** Ed25519 keys use a 32-byte seed (not the 64-byte expanded key). The seed is the same as the first 32 bytes of the identity's private key in µReticulum.

## Monocypher

The native module wraps [Monocypher 4.0.2](https://monocypher.org/) by Loup Vaillant. Monocypher is:
- Public domain (CC0 / 2-clause BSD)
- Designed for embedded systems
- 2 source files, ~3000 lines of portable C
- No dependencies, no dynamic allocation
- Audited

**Important:** Monocypher 4.x has two EdDSA implementations:
- `crypto_eddsa_*` (in `monocypher.c`) — uses **BLAKE2b** (NOT standard Ed25519)
- `crypto_ed25519_*` (in `optional/monocypher-ed25519.c`) — uses **SHA-512** (standard RFC 8032)

This module uses `crypto_ed25519_*` for RFC 8032 compatibility with reference Reticulum (which uses PyCA/OpenSSL Ed25519). Using `crypto_eddsa_*` would produce different keys and signatures.

## Troubleshooting

**`ValueError: incompatible .mpy file`** — The `.mpy` version doesn't match your firmware. Rebuild with the MicroPython source matching your firmware version.

**`ImportError: no module named 'ed25519_fast_xtensawin'`** — The `.mpy` file isn't on the device filesystem. Upload it with `mpremote cp`.

**`MemoryError` on import** — The native module needs ~46KB of RAM to load. On ESP32-S3 with PSRAM this is never an issue. On devices without PSRAM, ensure enough free heap.

**Link errors during build (`undefined symbol: memset`)** — Ensure `main.c` includes the inline `memset`/`memcpy`/`memcmp` implementations (they're there by default). The natmod environment has no libc.
