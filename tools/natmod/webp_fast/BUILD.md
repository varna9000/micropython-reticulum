# Building `webp_fast`

`webp_fast` is a MicroPython native module (`.mpy`) that wraps **libwebp 1.5.0**
(lossy encoder only) plus **TJpgDec** (ChaN, JPEG decode), for ESP32-S3 (`xtensawin`).
It provides `from_jpeg`, `encode_rgb`, `encode_rgb565`, `version` — see `main.c`.

The prebuilt module is committed at `firmware/lib/webp_fast_xtensawin.mpy`, so you only
need to rebuild if you change the source or target a different MicroPython version.

## Prerequisites
- **MicroPython source matching the firmware's exact version.** Native `.mpy` ABI is tied
  to the firmware via `mp_fun_table` — building against a different version crashes on
  import. The camera firmware here is **v1.27.0**, so:
  `git clone --depth 1 --branch v1.27.0 https://github.com/micropython/micropython ~/micropython-1.27`
- The Xtensa ESP32-S3 toolchain (from ESP-IDF), e.g.
  `~/.espressif/tools/xtensa-esp-elf/.../bin/xtensa-esp32s3-elf-`.

## ⚠️ Required `mpy_ld.py` patch (large multi-object modules)
`mpy_ld` (the natmod linker) has a bug that bites modules with many object files (libwebp
has ~100): GOT entries for section-relative refs are named only by the *generic* section
name (`.rodata`/`.text`/`.bss`), so refs from different objects **collide and dedupe to the
first object's section** — cross-wiring strings, data tables and static-function pointers,
which crashes at runtime. Patch `~/micropython-1.27/tools/mpy_ld.py`, in `build_got_xtensa`:

```python
# was: name = "{}+0x{:x}".format(s.section.name, existing)
name = "{}|{}+0x{:x}".format(s.section.filename, s.section.name, existing)
```

(Worth upstreaming to MicroPython. Without it, `webp_fast` builds and imports but any
libwebp call produces garbage / crashes.)

## libwebp source patches (already applied in `libwebp/`)
`mpy_ld` rejects writable data with relocations, so a few libwebp globals were made
`const`/flag-based, plus one latent-bug guard:
- `src/dsp/cpu.h` — `WEBP_DSP_INIT` self-pointer sentinel → a plain `static int` flag.
- `src/enc/vp8l_enc.c` — `hash_functions[]` table made `static`.
- `src/utils/thread_utils.c` — `g_worker_interface` made `const`; setter no-op.
- `sharpyuv/sharpyuv.c` — cpuinfo sentinel → `static int` flag.
- `src/enc/analysis_enc.c` — guard `AssignSegments` divide-by-zero (`total_weight==0`)
  and zero-init `map[]` (a latent upstream bug, hit when the alpha histogram is empty).

`tjpgd/` is ChaN's TJpgDec with the LVGL wrapper stripped (`JD_FORMAT=0` RGB888,
`JD_USE_SCALE=1`). **Note:** the LVGL variant emits pixels in B,G,R order (LVGL's
color format); this reached `WebPPictureImportRGB()` and swapped red/blue in every
camera image (pink skies, blue foliage). Fixed in `tjpgd.c` mcu_output() by
restoring stock ChaN R,G,B store order.

## Build
```sh
make CROSS=~/.espressif/tools/xtensa-esp-elf/<ver>/xtensa-esp-elf/bin/xtensa-esp32s3-elf-
```
(`MPY_DIR` in the Makefile must point at the matching MicroPython checkout; `CROSS` must be
passed on the command line because `dynruntime.mk` overrides a Makefile `CROSS` for
xtensawin. `LINK_RUNTIME=1` auto-links libgcc soft-float + libm.)

Output: `webp_fast.mpy`. Deploy as `firmware/lib/webp_fast_xtensawin.mpy` (the arch suffix
lets `peripherals/webp.py` pick the right build per platform).

## Notes
- The Makefile builds **lossy only** — VP8L/lossless encoder, SIMD, and decoder DSP are
  filtered out (`wf_vp8l_stub.c` satisfies the two referenced VP8L symbols).
- Allocations go through an arena (`wf_alloc.c`, one `m_malloc` per call); `wf_libc.c`
  provides `mem*`, `qsort`, etc.
