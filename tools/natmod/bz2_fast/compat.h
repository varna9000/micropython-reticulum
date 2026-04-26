/* compat.h — portable memcpy/memset/memcmp for natmod (no libc)
 *
 * Must be included BEFORE any standard headers via -include flag.
 * Provides inline implementations that cannot be overridden by libgcc.
 */
#ifndef _COMPAT_H
#define _COMPAT_H

#include <stdint.h>
#include <stddef.h>

static inline void *_mc_memcpy(void *dst, const void *src, size_t n) {
    uint8_t *d = (uint8_t *)dst;
    const uint8_t *s = (const uint8_t *)src;
    while (n--) *d++ = *s++;
    return dst;
}

static inline void *_mc_memset(void *s, int c, size_t n) {
    uint8_t *p = (uint8_t *)s;
    while (n--) *p++ = (uint8_t)c;
    return s;
}

static inline int _mc_memcmp(const void *a, const void *b, size_t n) {
    const uint8_t *pa = (const uint8_t *)a;
    const uint8_t *pb = (const uint8_t *)b;
    while (n--) {
        if (*pa != *pb) return *pa - *pb;
        pa++; pb++;
    }
    return 0;
}

/* Override standard functions — every call site uses our inlines */
#define memcpy  _mc_memcpy
#define memset  _mc_memset
#define memcmp  _mc_memcmp

#endif /* _COMPAT_H */
