# Build ed25519_fast native module for MicroPython
#
# Usage:
#   make ARCH=xtensawin                          # ESP32-S3
#   make ARCH=armv6m CROSS=arm-none-eabi-        # RP2040
#
# Result: ed25519_fast.mpy — upload to device filesystem

MPY_DIR = /Users/milen/micropython
MOD = ed25519_fast
SRC = main.c monocypher.c monocypher-ed25519.c libc_stubs.c
ARCH = xtensawin

CFLAGS += -Os -DNDEBUG -fno-builtin
LINK_RUNTIME = 1

# Default cross-compiler (ESP-IDF); override with CROSS=arm-none-eabi- for ARM
CROSS = /Users/milen/.espressif/tools/xtensa-esp-elf/esp-14.2.0_20260121/xtensa-esp-elf/bin/xtensa-esp32s3-elf-

include $(MPY_DIR)/py/dynruntime.mk
