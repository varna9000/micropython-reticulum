/* Real allocator for libwebp inside a natmod.
 *
 * One GC block (the arena) is taken per encode via wf_arena_begin and released
 * by wf_arena_end (so no m_malloc happens mid-encode, which would let the GC
 * collect libwebp's buffers). Within the arena we run a correct implicit-free-
 * list allocator (first-fit + coalescing) so free()/realloc() actually reclaim
 * memory — bounding usage to peak, not cumulative.
 *
 * Block layout (all 16-byte aligned):
 *   [ hdr: u32 size | u32 inuse | 8 pad ]  payload...  [ ftr: u32 size | 12 pad ]
 * 'size' is the whole block (hdr+payload+ftr). Payload = block + 16.
 */
#include "py/dynruntime.h"

#define WF_ALIGN   16u
#define WF_HDR     16u
#define WF_FTR     16u
#define WF_OVH     (WF_HDR + WF_FTR)

static uint8_t *g_arena = NULL;
static size_t   g_size = 0;
static int      g_oom = 0;

static inline size_t blk_size(uint8_t *b) { return *(uint32_t *)b; }
static inline int    blk_inuse(uint8_t *b) { return *(uint32_t *)(b + 4); }
static inline void   blk_set(uint8_t *b, size_t sz, int inuse) {
    *(uint32_t *)b = (uint32_t)sz;
    *(uint32_t *)(b + 4) = (uint32_t)inuse;
    *(uint32_t *)(b + sz - WF_FTR) = (uint32_t)sz;   /* footer mirrors size */
}
static inline size_t prev_ftr_size(uint8_t *b) { return *(uint32_t *)(b - WF_FTR); }

int wf_arena_begin(size_t bytes) {
    bytes = (bytes + WF_ALIGN - 1) & ~(size_t)(WF_ALIGN - 1);
    uint8_t *raw = m_malloc(bytes + WF_ALIGN);
    if (raw == NULL) return 0;
    /* align the arena start to 16 */
    g_arena = (uint8_t *)(((uintptr_t)raw + WF_ALIGN - 1) & ~(uintptr_t)(WF_ALIGN - 1));
    g_size = bytes;
    g_oom = 0;
    /* one big free block spanning the whole arena */
    blk_set(g_arena, g_size, 0);
    return 1;
}
void wf_arena_end(void) {
    /* the arena is one GC block; m_free expects the original pointer, but a
       MicroPython m_malloc block is GC-tracked and will be collected anyway.
       We deliberately do not m_free a shifted pointer; drop the reference and
       let GC reclaim it (the bytes result was already copied out). */
    g_arena = NULL; g_size = 0; g_oom = 0;
}
int    wf_arena_oom(void) { return g_oom; }

static void *find_fit(size_t need) {
    uint8_t *b = g_arena;
    uint8_t *end = g_arena + g_size;
    while (b < end) {
        size_t sz = blk_size(b);
        if (sz < WF_OVH || b + sz > end) return NULL;  /* corruption guard */
        if (!blk_inuse(b) && sz >= need) return b;
        b += sz;
    }
    return NULL;
}

void *malloc(size_t n) {
    if (g_arena == NULL) { g_oom = 1; return NULL; }
    size_t need = (n + WF_OVH + WF_ALIGN - 1) & ~(size_t)(WF_ALIGN - 1);
    if (need < n) { g_oom = 1; return NULL; }  /* overflow */
    uint8_t *b = find_fit(need);
    if (b == NULL) { g_oom = 1; return NULL; }
    size_t sz = blk_size(b);
    if (sz >= need + WF_OVH + WF_ALIGN) {       /* split off remainder */
        blk_set(b, need, 1);
        blk_set(b + need, sz - need, 0);
    } else {
        blk_set(b, sz, 1);
    }
    return b + WF_HDR;
}

void free(void *p) {
    if (p == NULL || g_arena == NULL) return;
    uint8_t *b = (uint8_t *)p - WF_HDR;
    size_t sz = blk_size(b);
    /* coalesce with next */
    uint8_t *nxt = b + sz;
    if (nxt < g_arena + g_size && !blk_inuse(nxt)) sz += blk_size(nxt);
    /* coalesce with prev */
    if (b > g_arena) {
        size_t psz = prev_ftr_size(b);
        uint8_t *prev = b - psz;
        if (prev >= g_arena && !blk_inuse(prev)) { b = prev; sz += psz; }
    }
    blk_set(b, sz, 0);
}

void *realloc(void *p, size_t n) {
    if (p == NULL) return malloc(n);
    if (n == 0) { free(p); return NULL; }
    uint8_t *b = (uint8_t *)p - WF_HDR;
    size_t cur = blk_size(b) - WF_OVH;       /* current payload capacity */
    if (n <= cur) return p;                  /* fits in place */
    void *q = malloc(n);
    if (q == NULL) return NULL;
    const uint8_t *s = p; uint8_t *d = q;
    for (size_t i = 0; i < cur; i++) d[i] = s[i];
    free(p);
    return q;
}

void *calloc(size_t a, size_t b) {
    size_t n = a * b;
    if (a != 0 && n / a != b) { g_oom = 1; return NULL; }  /* overflow */
    uint8_t *p = (uint8_t *)malloc(n);
    if (p) for (size_t i = 0; i < n; i++) p[i] = 0;
    return p;
}
