/* webp_fast — MicroPython native module: WebP encoder (libwebp) for ESP32-S3.
 *
 *   import webp_fast
 *   webp = webp_fast.from_jpeg(jpeg,    quality[, scale[, method[, arena_kb]]])
 *   webp = webp_fast.encode_rgb(rgb888, w, h, quality[, method[, arena_kb]])
 *   webp = webp_fast.encode_rgb565(rgb565, w, h, quality[, method[, arena_kb]])
 *   v    = webp_fast.version()
 *
 * Lossy-only WebP (quality-based). quality 0..100 (higher=better/larger),
 * method 0..6 (higher=slower/smaller). from_jpeg decodes the camera's hardware
 * JPEG with TJpgDec (ChaN) to RGB888 then encodes — the JPEG has already
 * filtered sensor noise, so it compresses far smaller than raw RGB565.
 * scale: 0=1/1, 1=1/2, 2=1/4, 3=1/8 downscale during decode. See the Makefile
 * for the trimmed libwebp source set.
 */
#include "py/dynruntime.h"
#include "src/webp/encode.h"
#include "tjpgd/tjpgd.h"

/* arena allocator (wf_alloc.c) */
extern int    wf_arena_begin(size_t bytes);
extern void   wf_arena_end(void);
extern int    wf_arena_oom(void);

/* libc shims used here (wf_alloc.c / wf_libc.c) */
extern void  *malloc(size_t n);
extern void  *memcpy(void *d, const void *s, size_t n);
extern void  *memset(void *d, int c, size_t n);

/* ---- shared encoder: RGB888 (already in the arena) -> WebP bytes|None ----
 * Assumes wf_arena_begin() is active; the caller owns the arena lifecycle. */
static mp_obj_t wf_encode_rgb888(const uint8_t *rgb, int w, int h,
                                 int quality, int method) {
    mp_obj_t res = mp_const_none;
    WebPConfig config;
    WebPPicture pic;
    WebPMemoryWriter wrt;     /* wrt.mem lives in the arena, kept reachable below */
    if (WebPConfigInit(&config) && WebPPictureInit(&pic)) {
        config.quality = (float)quality;
        config.method = method;   /* 0=fast/large .. 6=slow/small */
        config.low_memory = 1;    /* no token buffer: less RAM, simpler path */
        pic.width = w;
        pic.height = h;
        pic.use_argb = 0;         /* YUV pipeline */
        WebPMemoryWriterInit(&wrt);
        pic.writer = WebPMemoryWrite;
        pic.custom_ptr = &wrt;
        if (WebPPictureImportRGB(&pic, rgb, w * 3)) {
            if (WebPEncode(&config, &pic) && !wf_arena_oom()
                    && wrt.mem != NULL && wrt.size > 0) {
                res = mp_obj_new_bytes(wrt.mem, wrt.size);  /* copy before free */
            }
        }
        WebPPictureFree(&pic);
    }
    return res;
}

/* version() -> int : libwebp encoder version (0xMMmmrr) */
static mp_obj_t wf_version(void) {
    return mp_obj_new_int(WebPGetEncoderVersion());
}
static MP_DEFINE_CONST_FUN_OBJ_0(wf_version_obj, wf_version);

/* parse the common (method, arena_kb) option tail starting at arg index 'first' */
static void wf_parse_opts(size_t n_args, const mp_obj_t *args, size_t first,
                          int *method, size_t *arena) {
    *method = (n_args > first) ? mp_obj_get_int(args[first]) : 4;
    *arena = (n_args > first + 1)
        ? (size_t)mp_obj_get_int(args[first + 1]) * 1024u
        : (size_t)4u * 1024u * 1024u;
}

/* encode_rgb(rgb888, w, h, quality, [method, [arena_kb]]) -> bytes | None */
static mp_obj_t encode_rgb(size_t n_args, const mp_obj_t *args) {
    mp_buffer_info_t buf;
    mp_get_buffer_raise(args[0], &buf, MP_BUFFER_READ);
    int w = mp_obj_get_int(args[1]);
    int h = mp_obj_get_int(args[2]);
    int quality = mp_obj_get_int(args[3]);
    int method; size_t arena;
    wf_parse_opts(n_args, args, 4, &method, &arena);

    if (w <= 0 || h <= 0) mp_raise_ValueError(MP_ERROR_TEXT("bad dimensions"));
    if (buf.len < (size_t)w * (size_t)h * 3u)
        mp_raise_ValueError(MP_ERROR_TEXT("rgb buffer too small"));
    if (!wf_arena_begin(arena))
        mp_raise_ValueError(MP_ERROR_TEXT("arena alloc failed (raise arena_kb / free RAM)"));

    mp_obj_t res = wf_encode_rgb888((const uint8_t *)buf.buf, w, h, quality, method);
    wf_arena_end();
    return res;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(encode_rgb_obj, 4, 6, encode_rgb);

/* encode_rgb565(rgb565, w, h, quality, [method, [arena_kb]]) -> bytes | None
 * rgb565 framebuffer: w*h*2 bytes, big-endian (byte0 = high). Note: raw sensor
 * RGB565 keeps noise the JPEG path filters — prefer from_jpeg for small files. */
static mp_obj_t encode_rgb565(size_t n_args, const mp_obj_t *args) {
    mp_buffer_info_t buf;
    mp_get_buffer_raise(args[0], &buf, MP_BUFFER_READ);
    int w = mp_obj_get_int(args[1]);
    int h = mp_obj_get_int(args[2]);
    int quality = mp_obj_get_int(args[3]);
    int method; size_t arena;
    wf_parse_opts(n_args, args, 4, &method, &arena);

    if (w <= 0 || h <= 0) mp_raise_ValueError(MP_ERROR_TEXT("bad dimensions"));
    if (buf.len < (size_t)w * (size_t)h * 2u)
        mp_raise_ValueError(MP_ERROR_TEXT("rgb565 buffer too small"));
    if (!wf_arena_begin(arena))
        mp_raise_ValueError(MP_ERROR_TEXT("arena alloc failed (raise arena_kb / free RAM)"));

    mp_obj_t res = mp_const_none;
    uint8_t *rgb = (uint8_t *)malloc((size_t)w * h * 3);
    if (rgb != NULL) {
        const uint8_t *s = (const uint8_t *)buf.buf;
        uint8_t *d = rgb;
        int n = w * h;
        for (int i = 0; i < n; i++) {
            unsigned px = ((unsigned)s[0] << 8) | s[1];   /* big-endian 565 */
            s += 2;
            unsigned r = (px >> 11) & 0x1F, g = (px >> 5) & 0x3F, b = px & 0x1F;
            *d++ = (uint8_t)((r << 3) | (r >> 2));        /* 5->8 bit */
            *d++ = (uint8_t)((g << 2) | (g >> 4));        /* 6->8 bit */
            *d++ = (uint8_t)((b << 3) | (b >> 2));        /* 5->8 bit */
        }
        res = wf_encode_rgb888(rgb, w, h, quality, method);
    }
    wf_arena_end();
    return res;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(encode_rgb565_obj, 4, 6, encode_rgb565);

/* ---- TJpgDec glue: decode an in-memory JPEG into an RGB888 frame ---- */
typedef struct {
    const uint8_t *jpeg; size_t jpeg_len; size_t jpeg_pos;  /* input stream */
    uint8_t *rgb; int out_w, out_h;                         /* output frame */
} wf_jpeg_ctx;

static size_t wf_jpeg_in(JDEC *jd, uint8_t *buff, size_t nbyte) {
    wf_jpeg_ctx *c = (wf_jpeg_ctx *)jd->device;
    size_t avail = c->jpeg_len - c->jpeg_pos;
    if (nbyte > avail) nbyte = avail;
    if (buff) memcpy(buff, c->jpeg + c->jpeg_pos, nbyte);  /* read; else skip */
    c->jpeg_pos += nbyte;
    return nbyte;
}

static int wf_jpeg_out(JDEC *jd, void *bitmap, JRECT *rect) {
    wf_jpeg_ctx *c = (wf_jpeg_ctx *)jd->device;
    const uint8_t *src = (const uint8_t *)bitmap;
    int rw = rect->right - rect->left + 1;
    int rh = rect->bottom - rect->top + 1;
    for (int y = 0; y < rh; y++) {
        int oy = rect->top + y;
        if (oy >= c->out_h) break;
        int cw = rw;                                   /* clamp to frame width */
        if (rect->left + cw > c->out_w) cw = c->out_w - rect->left;
        if (cw <= 0) continue;
        uint8_t *dst = c->rgb + ((size_t)oy * c->out_w + rect->left) * 3;
        memcpy(dst, src + (size_t)y * rw * 3, (size_t)cw * 3);
    }
    return 1;  /* continue */
}

/* from_jpeg(jpeg, quality, [scale, [method, [arena_kb]]]) -> bytes | None */
static mp_obj_t from_jpeg(size_t n_args, const mp_obj_t *args) {
    mp_buffer_info_t buf;
    mp_get_buffer_raise(args[0], &buf, MP_BUFFER_READ);
    int quality = mp_obj_get_int(args[1]);
    int scale = (n_args > 2) ? mp_obj_get_int(args[2]) : 0;
    int method; size_t arena;
    wf_parse_opts(n_args, args, 3, &method, &arena);

    if (scale < 0 || scale > 3) mp_raise_ValueError(MP_ERROR_TEXT("scale must be 0..3"));
    if (buf.len < 4) mp_raise_ValueError(MP_ERROR_TEXT("jpeg too small"));
    if (!wf_arena_begin(arena))
        mp_raise_ValueError(MP_ERROR_TEXT("arena alloc failed (raise arena_kb / free RAM)"));

    mp_obj_t res = mp_const_none;
    const size_t pool_sz = 4096;          /* TJpgDec work area (JD_FASTDECODE=1) */
    void *pool = malloc(pool_sz);
    JDEC jd;
    wf_jpeg_ctx ctx;
    ctx.jpeg = (const uint8_t *)buf.buf;
    ctx.jpeg_len = buf.len;
    ctx.jpeg_pos = 0;
    ctx.rgb = NULL;
    if (pool != NULL && jd_prepare(&jd, wf_jpeg_in, pool, pool_sz, &ctx) == JDR_OK) {
        int out_w = jd.width >> scale;
        int out_h = jd.height >> scale;
        if (out_w > 0 && out_h > 0) {
            ctx.out_w = out_w;
            ctx.out_h = out_h;
            ctx.rgb = (uint8_t *)malloc((size_t)out_w * out_h * 3);
            if (ctx.rgb != NULL) {
                memset(ctx.rgb, 0, (size_t)out_w * out_h * 3);
                if (jd_decomp(&jd, wf_jpeg_out, (uint8_t)scale) == JDR_OK) {
                    res = wf_encode_rgb888(ctx.rgb, out_w, out_h, quality, method);
                }
            }
        }
    }
    wf_arena_end();
    return res;
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(from_jpeg_obj, 2, 5, from_jpeg);

mp_obj_t mpy_init(mp_obj_fun_bc_t *self, size_t n_args, size_t n_kw, mp_obj_t *args) {
    MP_DYNRUNTIME_INIT_ENTRY
    mp_store_global(MP_QSTR_version, MP_OBJ_FROM_PTR(&wf_version_obj));
    mp_store_global(MP_QSTR_from_jpeg, MP_OBJ_FROM_PTR(&from_jpeg_obj));
    mp_store_global(MP_QSTR_encode_rgb, MP_OBJ_FROM_PTR(&encode_rgb_obj));
    mp_store_global(MP_QSTR_encode_rgb565, MP_OBJ_FROM_PTR(&encode_rgb565_obj));
    MP_DYNRUNTIME_INIT_EXIT
}
