# Build webp_fast native module (WebP encoder) for ESP32-S3 (xtensawin).
#
#   make CROSS=/path/to/xtensa-esp32s3-elf-
#
# Result: webp_fast.mpy  -> upload to device /lib

MPY_DIR = /Users/milen/micropython-1.27
MOD = webp_fast
ARCH = xtensawin
LINK_RUNTIME = 1

# libwebp LOSSY-ONLY encoder subset (drop the large VP8L/lossless code to keep
# the natmod small enough for mpy_ld's xtensawin relocation range). We only need
# quality-based (lossy) WebP. SIMD and decoder files are excluded too.
# Genuinely lossless-encoder-only (VP8L) — droppable for lossy WebP:
LOSSLESS = libwebp/src/enc/vp8l_enc.c \
           libwebp/src/enc/backward_references_enc.c \
           libwebp/src/enc/backward_references_cost_enc.c \
           libwebp/src/enc/histogram_enc.c \
           libwebp/src/enc/near_lossless_enc.c \
           libwebp/src/enc/predictor_enc.c \
           libwebp/src/dsp/lossless_enc.c
SIMD = $(wildcard libwebp/src/dsp/*_sse2.c) $(wildcard libwebp/src/dsp/*_sse41.c) \
       $(wildcard libwebp/src/dsp/*_neon.c) $(wildcard libwebp/src/dsp/*_msa.c) \
       $(wildcard libwebp/src/dsp/*_mips32.c) $(wildcard libwebp/src/dsp/*_mips_dsp_r2.c) \
       $(wildcard libwebp/sharpyuv/*_sse2.c) $(wildcard libwebp/sharpyuv/*_neon.c)
# Keep dec.c/lossless.c/upsampling.c — shared loop-filter / transform /
# RGB-conversion DSP the encoder's color-space prep uses.
WEBP_SRC = $(filter-out $(LOSSLESS) $(SIMD), \
             $(wildcard libwebp/src/enc/*.c) \
             $(wildcard libwebp/src/dsp/*.c) \
             $(wildcard libwebp/src/utils/*.c) \
             $(wildcard libwebp/sharpyuv/*.c)) \
           wf_vp8l_stub.c

SRC = main.c wf_alloc.c wf_libc.c tjpgd/tjpgd.c $(WEBP_SRC)

# -Ilibwebp so "src/..." and "sharpyuv/..." includes resolve.
# -Wno-error: libwebp emits warnings; -fno-builtin: keep our mem* symbols.
CFLAGS_EXTRA += -Ilibwebp -fno-builtin -Wno-error -DNDEBUG -DWEBP_DISABLE_STATS

include $(MPY_DIR)/py/dynruntime.mk
