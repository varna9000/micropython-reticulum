/* Minimal libc for libwebp inside a natmod. mem* are real external symbols
   (gcc emits calls to them for struct/array copies); errno/abort are stubs. */
#include <stddef.h>

void *memcpy(void *d, const void *s, size_t n) {
    unsigned char *a = d; const unsigned char *b = s;
    while (n--) *a++ = *b++; return d;
}
void *memset(void *d, int c, size_t n) {
    unsigned char *a = d; while (n--) *a++ = (unsigned char)c; return d;
}
void *memmove(void *d, const void *s, size_t n) {
    unsigned char *a = d; const unsigned char *b = s;
    if (a < b) { while (n--) *a++ = *b++; }
    else { a += n; b += n; while (n--) *--a = *--b; }
    return d;
}
int memcmp(const void *a, const void *b, size_t n) {
    const unsigned char *pa = a, *pb = b;
    while (n--) { if (*pa != *pb) return *pa - *pb; pa++; pb++; }
    return 0;
}

/* newlib libm error path */
int *__errno(void) { static int _e; return &_e; }

/* libwebp only abort()s on internal asserts, which NDEBUG disables. */
void abort(void) { for (;;) {} }

int abs(int x) { return x < 0 ? -x : x; }

void *bsearch(const void *key, const void *base, size_t n, size_t sz,
              int (*cmp)(const void *, const void *)) {
    const char *b = base;
    while (n) {
        size_t mid = n >> 1;
        const char *p = b + mid * sz;
        int c = cmp(key, p);
        if (c == 0) return (void *)p;
        if (c > 0) { b = p + sz; n -= mid + 1; }
        else { n = mid; }
    }
    return 0;
}

/* shell sort: O(n log^2 n), iterative, in-place; libwebp only sorts small arrays */
void qsort(void *base, size_t n, size_t sz,
           int (*cmp)(const void *, const void *)) {
    char *a = base;
    for (size_t gap = n / 2; gap > 0; gap /= 2) {
        for (size_t i = gap; i < n; i++) {
            for (size_t j = i; j >= gap &&
                 cmp(a + (j - gap) * sz, a + j * sz) > 0; j -= gap) {
                char *x = a + (j - gap) * sz, *y = a + j * sz;
                for (size_t k = 0; k < sz; k++) { char t = x[k]; x[k] = y[k]; y[k] = t; }
            }
        }
    }
}
