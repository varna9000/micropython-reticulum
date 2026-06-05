/* Stubs for the VP8L (lossless) encoder entry points referenced by the lossy
 * build (webp_enc.c, alpha_enc.c). We build lossy-only; with quality-based
 * encoding config->lossless is 0 so these are never reached at runtime — they
 * exist only to satisfy the linker. Returning failure is safe.
 *
 * Real signatures:
 *   int VP8LEncodeImage(const WebPConfig*, const WebPPicture*);
 *   WebPEncodingError VP8LEncodeStream(const WebPConfig*, const WebPPicture*,
 *                                      VP8LBitWriter*, int use_cache);
 * void* used here to avoid pulling the internal headers; only the symbol
 * name/return-size matters for linking.
 */
int VP8LEncodeImage(const void *config, const void *picture) {
    (void)config; (void)picture;
    return 0;  /* 0 = failure */
}

int VP8LEncodeStream(const void *config, const void *picture,
                     void *bw, int use_cache) {
    (void)config; (void)picture; (void)bw; (void)use_cache;
    return 0;  /* VP8_ENC_ERROR_OUT_OF_MEMORY-ish; never reached for lossy */
}
