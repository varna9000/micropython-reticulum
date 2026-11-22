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

Rename with the architecture suffix and copy it into the repo's `firmware/lib/` (deployed to the board's `/lib`; `mpremote cp -r firmware/ :` does it). **On ESP32 the same build must be installed under THREE names, all identical** - `firmware/lib/ed25519_fast_xtensawin.mpy`, `firmware/lib/ed25519_iram.mpy` (the loader tries this one FIRST, because a file-system natmod runs from IRAM, ~80x faster than the built-in/XIP name) and `tools/natmod/ed25519_fast/ed25519_fast_xtensawin.mpy`; `ed25519_iram.mpy` is not a separate build, just the same file under another name. `firmware/tests/test_native_module_files.py` fails if they differ or lack `hashcash`/`x25519_stamp2`; the loader also prefers a module that has those functions over a stale one.

```bash
# ESP32-S3
cp ed25519_fast.mpy tools/natmod/ed25519_fast/ed25519_fast_xtensawin.mpy
cp ed25519_fast.mpy firmware/lib/ed25519_fast_xtensawin.mpy
cp ed25519_fast.mpy firmware/lib/ed25519_iram.mpy

# RP2040
cp ed25519_fast.mpy firmware/lib/ed25519_fast_armv6m.mpy
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

## Stamp functions (Reticulum 1.6)

Besides `sign`/`verify`/`publickey`/`x25519`/`x25519_publickey` the module exports (used by `urns/stamps.py`; every one is feature-detected, so an older module just means the pure-Python fallback):

- `x25519_stamp2_init(buf)` / `x25519_stamp2(seed, material, target32, max, buf[, hw])` - link-request key-stamp search: an X25519 key whose public key satisfies `sha256(material + pub) <= target`. `buf` is a 4-byte-aligned bytearray of `16 + 120*(n+1)` bytes, initialised once.
- `hashcash(material, target32, stamp_len, max_tries, seed32[, hw])` -> `(stamp or None, tried)` - generic `sha256(material + stamp) <= target`, stamp = 4-byte counter + `seed[4:stamp_len]`, `stamp_len` 5..32. One call is one bounded slice; call again with a fresh seed.
- `hwselftest(material, count)` -> `(bad, bad_words)`, `hwstats(reset)` -> `(hw_hashes, mismatches, found_busy, taken_from_us)` - diagnostics for the hardware path (all zeros off-ESP32-S3).

`hw=1` filters candidates on the ESP32-S3 SHA accelerator (direct register access; every hit is re-verified in software). It backs off to software when the engine is held by the firmware, and the SHA clock is held on from boot until WiFi has joined, so it only engages afterwards. That code is compiled only under `__XTENSA__`; Python must pass `hw` only on an ESP32-S3 (`stamps.HW_SHA`, config `stamps_hw_sha`, default off) because original-ESP32 builds share the `xtensawin` arch but not the register map.

Build notes: `make ARCH=xtensawin OPT=-O2` (the Makefile takes `OPT`, default `-Os`; the shipped module is `-O2`). Needs the xtensa toolchain on `PATH`, `pyelftools` and `ar` in the Python that runs `mpy_ld.py` (a venv with both works), and `MPY_DIR` pointing at a MicroPython checkout (the one used here carries the local `mpy_ld.py` patch for large multi-object modules; whether this module also needs it was not tested). `.bss` variables are not allowed on `armv6m` (the linker rejects them), which is why the non-Xtensa hw stubs have no static state. Check the ARM build with the command above (it compiles and links the stamp code; 54 KB at `-Os`, not shipped). Current `ed25519_fast_xtensawin.mpy` (`-O2`): 66,683 bytes.
