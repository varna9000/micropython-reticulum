/*
 * bz2_fast — Native C bz2 decompressor for MicroPython (natmod)
 *
 * Ported from µReticulum's bz2dec.py (which was ported from pyflate).
 * Provides ~100-150x speedup over pure Python on ESP32.
 *
 * API: bz2_fast.decompress(compressed_bytes) -> bytes
 *
 * Memory: Peak ~120KB for 16KB resource (BWT pointers dominate).
 * Uses m_malloc/m_free from MicroPython heap.
 */

#include "py/dynruntime.h"

/* compat.h provides memcpy, memset, memcmp */
#include "compat.h"

/* --- Configuration --- */
#define MAX_GROUPS      6
#define MAX_SYMBOLS     258     /* max 256 chars + RUNA + RUNB */
#define MAX_HUFCODE_BITS 20
#define MAX_SELECTORS   32768
#define MAX_OUTPUT      (16384 + 1024)  /* MAX_RESOURCE_SIZE + margin */

/* --- Bitfield reader --- */
typedef struct {
    const uint8_t *data;
    uint32_t len;
    uint32_t pos;
    uint32_t bits;
    uint32_t bitfield;
} bitreader_t;

static void br_init(bitreader_t *br, const uint8_t *data, uint32_t len) {
    br->data = data;
    br->len = len;
    br->pos = 0;
    br->bits = 0;
    br->bitfield = 0;
}

static uint32_t br_readbits(bitreader_t *br, int n) {
    while (br->bits < (uint32_t)n) {
        if (br->pos >= br->len) {
            mp_raise_ValueError(MP_ERROR_TEXT("bz2: unexpected end of data"));
        }
        br->bitfield = (br->bitfield << 8) | br->data[br->pos++];
        br->bits += 8;
    }
    br->bits -= n;
    uint32_t r = (br->bitfield >> br->bits) & ((1u << n) - 1);
    br->bitfield &= (1u << br->bits) - 1;
    return r;
}

static uint32_t br_snoopbits(bitreader_t *br, int n) {
    while (br->bits < (uint32_t)n) {
        if (br->pos >= br->len) {
            mp_raise_ValueError(MP_ERROR_TEXT("bz2: unexpected end of data"));
        }
        br->bitfield = (br->bitfield << 8) | br->data[br->pos++];
        br->bits += 8;
    }
    return (br->bitfield >> (br->bits - n)) & ((1u << n) - 1);
}

static void br_align(bitreader_t *br) {
    int skip = br->bits & 7;
    if (skip) br_readbits(br, skip);
}

/* --- Huffman table --- */
typedef struct {
    uint16_t code;      /* original symbol index */
    uint8_t  bits;      /* bit length */
    uint16_t symbol;    /* canonical code value */
} huff_entry_t;

typedef struct {
    huff_entry_t entries[MAX_SYMBOLS];
    int count;
    /* Fast lookup: for each bit length, start index and count in entries[] */
    int bl_start[MAX_HUFCODE_BITS + 1];
    int bl_count[MAX_HUFCODE_BITS + 1];
    int min_bits;
    int max_bits;
} huff_table_t;

/* Sort entries by (bits, code) — simple insertion sort, count is small */
static void huff_sort(huff_entry_t *e, int n) {
    for (int i = 1; i < n; i++) {
        huff_entry_t key = e[i];
        int j = i - 1;
        while (j >= 0 && (e[j].bits > key.bits ||
               (e[j].bits == key.bits && e[j].code > key.code))) {
            e[j + 1] = e[j];
            j--;
        }
        e[j + 1] = key;
    }
}

static void huff_build(huff_table_t *ht, const uint8_t *lengths, int n_symbols) {
    ht->count = 0;
    for (int i = 0; i < n_symbols; i++) {
        if (lengths[i]) {
            ht->entries[ht->count].code = (uint16_t)i;
            ht->entries[ht->count].bits = lengths[i];
            ht->entries[ht->count].symbol = 0;
            ht->count++;
        }
    }
    huff_sort(ht->entries, ht->count);

    /* Assign canonical codes */
    int bits = -1;
    int symbol = -1;
    for (int i = 0; i < ht->count; i++) {
        symbol++;
        if (ht->entries[i].bits != bits) {
            symbol <<= (ht->entries[i].bits - bits);
            bits = ht->entries[i].bits;
        }
        ht->entries[i].symbol = (uint16_t)symbol;
    }

    /* Build bit-length index */
    memset(ht->bl_start, 0, sizeof(ht->bl_start));
    memset(ht->bl_count, 0, sizeof(ht->bl_count));
    ht->min_bits = MAX_HUFCODE_BITS;
    ht->max_bits = 0;

    for (int i = 0; i < ht->count; i++) {
        int b = ht->entries[i].bits;
        if (b < ht->min_bits) ht->min_bits = b;
        if (b > ht->max_bits) ht->max_bits = b;
        ht->bl_count[b]++;
    }

    /* Compute start indices per bit length */
    int idx = 0;
    for (int b = 0; b <= MAX_HUFCODE_BITS; b++) {
        ht->bl_start[b] = idx;
        idx += ht->bl_count[b];
    }
}

static int huff_find(huff_table_t *ht, bitreader_t *br) {
    for (int b = ht->min_bits; b <= ht->max_bits; b++) {
        if (ht->bl_count[b] == 0) continue;
        uint32_t bits_val = br_snoopbits(br, b);
        int start = ht->bl_start[b];
        int end = start + ht->bl_count[b];
        for (int i = start; i < end; i++) {
            if (ht->entries[i].symbol == bits_val) {
                br_readbits(br, b);
                return ht->entries[i].code;
            }
        }
    }
    mp_raise_ValueError(MP_ERROR_TEXT("bz2: huffman symbol not found"));
    return -1;
}

/* --- Move-to-front --- */
static void mtf(uint8_t *list, int c) {
    uint8_t v = list[c];
    for (int i = c; i > 0; i--) {
        list[i] = list[i - 1];
    }
    list[0] = v;
}

/* --- Inverse BWT --- */
static uint8_t *bwt_reverse(const uint8_t *data, int n, int end, int *out_len) {
    if (n == 0) {
        *out_len = 0;
        return NULL;
    }

    uint32_t counts[256];
    uint32_t base[256];
    memset(counts, 0, sizeof(counts));

    for (int i = 0; i < n; i++) {
        counts[data[i]]++;
    }

    uint32_t total = 0;
    for (int i = 0; i < 256; i++) {
        base[i] = total;
        total += counts[i];
    }

    uint32_t *pointers = m_malloc(n * sizeof(uint32_t));
    for (int i = 0; i < n; i++) {
        uint8_t s = data[i];
        pointers[base[s]] = i;
        base[s]++;
    }

    uint8_t *out = m_malloc(n);
    for (int i = 0; i < n; i++) {
        end = pointers[end];
        out[i] = data[end];
    }

    m_free(pointers);
    *out_len = n;
    return out;
}

/* --- Main decompressor --- */
static int bz2_decompress(const uint8_t *input, size_t input_len,
                          uint8_t **output, size_t *output_len) {
    /* Check magic "BZ" */
    if (input_len < 4 || input[0] != 'B' || input[1] != 'Z') {
        mp_raise_ValueError(MP_ERROR_TEXT("bz2: bad magic"));
    }

    bitreader_t br;
    br_init(&br, input + 2, input_len - 2);

    /* Method: must be 'h' (0x68) */
    uint32_t method = br_readbits(&br, 8);
    if (method != 0x68) {
        mp_raise_ValueError(MP_ERROR_TEXT("bz2: unknown method"));
    }

    /* Block size: '1'-'9' */
    uint32_t blocksize = br_readbits(&br, 8);
    if (blocksize < 0x31 || blocksize > 0x39) {
        mp_raise_ValueError(MP_ERROR_TEXT("bz2: unknown blocksize"));
    }

    /* Output buffer — pre-allocate MAX_OUTPUT */
    uint8_t *out_buf = m_malloc(MAX_OUTPUT);
    size_t out_pos = 0;

    while (1) {
        /* Read 48-bit block type */
        uint32_t bt_hi = br_readbits(&br, 24);
        uint32_t bt_lo = br_readbits(&br, 24);
        uint64_t blocktype = ((uint64_t)bt_hi << 24) | bt_lo;

        /* CRC (skip) */
        br_readbits(&br, 32);

        if (blocktype == 0x314159265359ULL) {
            /* --- Data block --- */
            if (br_readbits(&br, 1)) {
                mp_raise_ValueError(MP_ERROR_TEXT("bz2: randomised not supported"));
            }

            int pointer = (int)br_readbits(&br, 24);

            /* Read used character bitmap */
            uint32_t used_map = br_readbits(&br, 16);
            uint8_t used[256];
            int n_used = 0;
            memset(used, 0, sizeof(used));

            uint32_t mask = 1u << 15;
            int idx = 0;
            while (mask > 0) {
                if (used_map & mask) {
                    uint32_t bitmap = br_readbits(&br, 16);
                    uint32_t bit = 1u << 15;
                    while (bit > 0) {
                        if (bitmap & bit) {
                            used[idx] = 1;
                            n_used++;
                        }
                        idx++;
                        bit >>= 1;
                    }
                } else {
                    idx += 16;
                }
                mask >>= 1;
            }

            /* Read selectors */
            int n_groups = (int)br_readbits(&br, 3);
            int n_selectors = (int)br_readbits(&br, 15);

            uint8_t mtf_groups[MAX_GROUPS];
            for (int i = 0; i < n_groups; i++) mtf_groups[i] = i;

            uint8_t *selectors = m_malloc(n_selectors);
            for (int i = 0; i < n_selectors; i++) {
                int c = 0;
                while (br_readbits(&br, 1)) c++;
                mtf(mtf_groups, c);
                selectors[i] = mtf_groups[0];
            }

            /* Read Huffman tables */
            int n_symbols = n_used + 2;
            huff_table_t *tables = m_malloc(n_groups * sizeof(huff_table_t));

            for (int g = 0; g < n_groups; g++) {
                int length = (int)br_readbits(&br, 5);
                uint8_t lengths[MAX_SYMBOLS];

                for (int s = 0; s < n_symbols; s++) {
                    while (br_readbits(&br, 1)) {
                        length -= ((int)br_readbits(&br, 1) * 2) - 1;
                    }
                    lengths[s] = (uint8_t)length;
                }
                huff_build(&tables[g], lengths, n_symbols);
            }

            /* Build favourites list */
            uint8_t favourites[256];
            int n_fav = 0;
            for (int i = 0; i < 256; i++) {
                if (used[i]) favourites[n_fav++] = (uint8_t)i;
            }

            /* Decode block symbols */
            int buf_cap = 4096;
            uint8_t *buf = m_malloc(buf_cap);
            int buf_len = 0;

            int sel_ptr = 0;
            int decoded = 0;
            int repeat = 0;
            int repeat_power = 0;
            huff_table_t *t = NULL;

            while (1) {
                decoded--;
                if (decoded <= 0) {
                    decoded = 50;
                    if (sel_ptr < n_selectors) {
                        t = &tables[selectors[sel_ptr]];
                        sel_ptr++;
                    }
                }

                int r = huff_find(t, &br);

                if (r <= 1) {
                    if (repeat == 0) repeat_power = 1;
                    repeat += repeat_power << r;
                    repeat_power <<= 1;
                    continue;
                } else if (repeat > 0) {
                    /* Expand repeat */
                    int need = buf_len + repeat;
                    while (need > buf_cap) {
                        buf_cap *= 2;
                        buf = m_realloc(buf, buf_cap);
                    }
                    memset(buf + buf_len, favourites[0], repeat);
                    buf_len += repeat;
                    repeat = 0;
                }

                if (r == n_symbols - 1) {
                    break;  /* End of block */
                } else {
                    mtf(favourites, r - 1);
                    if (buf_len >= buf_cap) {
                        buf_cap *= 2;
                        buf = m_realloc(buf, buf_cap);
                    }
                    buf[buf_len++] = favourites[0];
                }
            }

            m_free(selectors);
            m_free(tables);

            /* Inverse BWT */
            int decoded_len = 0;
            uint8_t *decoded_block = bwt_reverse(buf, buf_len, pointer, &decoded_len);
            m_free(buf);

            /* Inverse RLE — write directly to output */
            int i = 0;
            while (i < decoded_len) {
                if (i < decoded_len - 4 &&
                    decoded_block[i] == decoded_block[i + 1] &&
                    decoded_block[i + 1] == decoded_block[i + 2] &&
                    decoded_block[i + 2] == decoded_block[i + 3]) {
                    uint8_t v = decoded_block[i];
                    int count = decoded_block[i + 4] + 4;
                    /* Grow output if needed */
                    while (out_pos + count > MAX_OUTPUT) {
                        mp_raise_ValueError(MP_ERROR_TEXT("bz2: output too large"));
                    }
                    memset(out_buf + out_pos, v, count);
                    out_pos += count;
                    i += 5;
                } else {
                    if (out_pos >= MAX_OUTPUT) {
                        mp_raise_ValueError(MP_ERROR_TEXT("bz2: output too large"));
                    }
                    out_buf[out_pos++] = decoded_block[i];
                    i++;
                }
            }

            if (decoded_block) m_free(decoded_block);

        } else if (blocktype == 0x177245385090ULL) {
            /* End of stream */
            br_align(&br);
            break;
        } else {
            mp_raise_ValueError(MP_ERROR_TEXT("bz2: unknown blocktype"));
        }
    }

    *output = out_buf;
    *output_len = out_pos;
    return 0;
}

/* --- BZ2 CRC-32 (big-endian polynomial) --- */
static uint32_t bz2_crc32(const uint8_t *data, size_t len) {
    uint32_t crc = 0xFFFFFFFF;
    for (size_t i = 0; i < len; i++) {
        crc ^= (uint32_t)data[i] << 24;
        for (int j = 0; j < 8; j++) {
            if (crc & 0x80000000)
                crc = (crc << 1) ^ 0x04C11DB7;
            else
                crc <<= 1;
        }
    }
    return crc ^ 0xFFFFFFFF;
}

/* --- Bit writer for compressor --- */
typedef struct {
    uint8_t *buf;
    size_t cap;
    size_t pos;
    uint32_t bits;
    uint64_t bitfield;
} bitwriter_t;

static void bw_init(bitwriter_t *bw, uint8_t *buf, size_t cap) {
    bw->buf = buf; bw->cap = cap; bw->pos = 0;
    bw->bits = 0; bw->bitfield = 0;
}

static void bw_write(bitwriter_t *bw, uint32_t value, int n) {
    bw->bitfield = (bw->bitfield << n) | (value & ((1ULL << n) - 1));
    bw->bits += n;
    while (bw->bits >= 8) {
        bw->bits -= 8;
        if (bw->pos < bw->cap)
            bw->buf[bw->pos++] = (bw->bitfield >> bw->bits) & 0xFF;
        bw->bitfield &= (1ULL << bw->bits) - 1;
    }
}

static size_t bw_flush(bitwriter_t *bw) {
    if (bw->bits > 0 && bw->pos < bw->cap)
        bw->buf[bw->pos++] = (bw->bitfield << (8 - bw->bits)) & 0xFF;
    return bw->pos;
}

/* --- BWT suffix comparison + Shell sort --- */
static int bwt_cmp(const uint8_t *block, int len, int ia, int ib) {
    for (int k = 0; k < len; k++) {
        int ca = block[(ia + k) % len];
        int cb = block[(ib + k) % len];
        if (ca != cb) return ca - cb;
    }
    return 0;
}

static void sort_indices(int *arr, int n, const uint8_t *block, int blen) {
    /* Shell sort — good enough for n <= 16K */
    int gap = n / 2;
    while (gap > 0) {
        for (int i = gap; i < n; i++) {
            int tmp = arr[i];
            int j = i;
            while (j >= gap && bwt_cmp(block, blen, arr[j - gap], tmp) > 0) {
                arr[j] = arr[j - gap];
                j -= gap;
            }
            arr[j] = tmp;
        }
        gap /= 2;
    }
}

/* --- BZ2 Compressor --- */
static int bz2_compress(const uint8_t *input, size_t input_len,
                        uint8_t **output, size_t *output_len) {
    if (input_len == 0 || input_len > MAX_OUTPUT) {
        mp_raise_ValueError(MP_ERROR_TEXT("bz2: invalid input size"));
    }

    /* RLE1: encode runs of 4+ identical bytes */
    uint8_t *rle = m_malloc(input_len + input_len / 4 + 16);
    int rle_len = 0;
    int i = 0;
    while (i < (int)input_len) {
        uint8_t ch = input[i];
        int run = 1;
        while (i + run < (int)input_len && input[i + run] == ch && run < 259)
            run++;
        if (run >= 4) {
            rle[rle_len++] = ch; rle[rle_len++] = ch;
            rle[rle_len++] = ch; rle[rle_len++] = ch;
            rle[rle_len++] = (uint8_t)(run - 4);
            i += run;
        } else {
            rle[rle_len++] = ch;
            i++;
        }
    }

    /* Used character bitmap */
    uint8_t used[256];
    memset(used, 0, 256);
    for (i = 0; i < rle_len; i++) used[rle[i]] = 1;
    int n_used = 0;
    for (i = 0; i < 256; i++) if (used[i]) n_used++;

    /* Forward BWT */
    int *indices = m_malloc(rle_len * sizeof(int));
    for (i = 0; i < rle_len; i++) indices[i] = i;
    sort_indices(indices, rle_len, rle, rle_len);

    int pointer = 0;
    for (i = 0; i < rle_len; i++) {
        if (indices[i] == 0) { pointer = i; break; }
    }

    uint8_t *last_col = m_malloc(rle_len);
    for (i = 0; i < rle_len; i++)
        last_col[i] = rle[(indices[i] + rle_len - 1) % rle_len];
    m_free(indices);

    /* MTF encode on used characters (favourites list) */
    uint8_t favourites[256];
    int n_fav = 0;
    for (i = 0; i < 256; i++) if (used[i]) favourites[n_fav++] = (uint8_t)i;

    int *mtf_out = m_malloc(rle_len * sizeof(int));
    for (i = 0; i < rle_len; i++) {
        uint8_t b = last_col[i];
        int idx = 0;
        while (favourites[idx] != b) idx++;
        mtf_out[i] = idx;
        /* Move to front */
        uint8_t v = favourites[idx];
        for (int j = idx; j > 0; j--) favourites[j] = favourites[j - 1];
        favourites[0] = v;
    }
    m_free(last_col);

    /* RLE2: zero-run encoding (bijective base-2) */
    int eob = n_used + 1;
    int *encoded = m_malloc((rle_len * 2 + 16) * sizeof(int));
    int enc_len = 0;
    int run = 0;
    for (i = 0; i < rle_len; i++) {
        if (mtf_out[i] == 0) {
            run++;
        } else {
            if (run > 0) {
                int r = run;
                while (r > 0) {
                    r--;
                    encoded[enc_len++] = r & 1; /* 0=RUNA, 1=RUNB */
                    r >>= 1;
                }
                run = 0;
            }
            encoded[enc_len++] = mtf_out[i] + 1;
        }
    }
    if (run > 0) {
        int r = run;
        while (r > 0) { r--; encoded[enc_len++] = r & 1; r >>= 1; }
    }
    encoded[enc_len++] = eob;
    m_free(mtf_out);
    m_free(rle);

    /* Flat Huffman: all symbols same bit length */
    int n_syms = eob + 1;
    int code_len = 1;
    while ((1 << code_len) < n_syms) code_len++;

    /* Write output — worst case: flat Huffman can expand data by code_len/8 ratio
       plus headers (~100 bytes). Allocate generously. */
    size_t out_cap = input_len * 2 + 512;
    uint8_t *out_buf = m_malloc(out_cap);
    /* Header "BZh9" */
    out_buf[0] = 'B'; out_buf[1] = 'Z'; out_buf[2] = 'h'; out_buf[3] = '9';

    bitwriter_t bw;
    bw_init(&bw, out_buf + 4, out_cap - 4);

    uint32_t block_crc = bz2_crc32(input, input_len);

    /* Block magic */
    bw_write(&bw, 0x314159265359ULL >> 24, 24);
    bw_write(&bw, 0x314159265359ULL & 0xFFFFFF, 24);
    bw_write(&bw, block_crc, 32);
    bw_write(&bw, 0, 1); /* not randomised */
    bw_write(&bw, pointer, 24);

    /* Used bitmap (2-level) */
    uint32_t used_groups = 0;
    for (int g = 0; g < 16; g++) {
        for (int k = 0; k < 16; k++) {
            if (used[g * 16 + k]) { used_groups |= 1u << (15 - g); break; }
        }
    }
    bw_write(&bw, used_groups, 16);
    for (int g = 0; g < 16; g++) {
        if (used_groups & (1u << (15 - g))) {
            uint32_t bitmap = 0;
            for (int k = 0; k < 16; k++)
                if (used[g * 16 + k]) bitmap |= 1u << (15 - k);
            bw_write(&bw, bitmap, 16);
        }
    }

    /* 2 groups (bzip2 requires minimum 2), both identical flat tables.
       All selectors point to group 0. */
    bw_write(&bw, 2, 3);
    int n_sel = (enc_len + 49) / 50;
    bw_write(&bw, n_sel, 15);
    for (i = 0; i < n_sel; i++) bw_write(&bw, 0, 1);  /* unary 0 = group 0 */

    /* Two identical Huffman tables: flat, delta-encoded */
    for (int g = 0; g < 2; g++) {
        bw_write(&bw, code_len, 5);
        for (i = 0; i < n_syms; i++) bw_write(&bw, 0, 1);
    }

    /* Encoded data */
    for (i = 0; i < enc_len; i++) bw_write(&bw, encoded[i], code_len);
    m_free(encoded);

    /* End-of-stream */
    bw_write(&bw, 0x177245385090ULL >> 24, 24);
    bw_write(&bw, 0x177245385090ULL & 0xFFFFFF, 24);
    bw_write(&bw, block_crc, 32); /* combined CRC = block CRC for single block */

    size_t bitstream_len = bw_flush(&bw);
    *output = out_buf;
    *output_len = 4 + bitstream_len;
    return 0;
}

/* --- MicroPython binding --- */

static mp_obj_t mod_decompress(mp_obj_t data_obj) {
    mp_buffer_info_t buf;
    mp_get_buffer_raise(data_obj, &buf, MP_BUFFER_READ);

    uint8_t *output = NULL;
    size_t output_len = 0;

    bz2_decompress(buf.buf, buf.len, &output, &output_len);

    mp_obj_t result = mp_obj_new_bytes(output, output_len);
    m_free(output);
    return result;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_decompress_obj, mod_decompress);

static mp_obj_t mod_compress(mp_obj_t data_obj) {
    mp_buffer_info_t buf;
    mp_get_buffer_raise(data_obj, &buf, MP_BUFFER_READ);

    uint8_t *output = NULL;
    size_t output_len = 0;

    bz2_compress(buf.buf, buf.len, &output, &output_len);

    mp_obj_t result = mp_obj_new_bytes(output, output_len);
    m_free(output);
    return result;
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_compress_obj, mod_compress);

mp_obj_t mpy_init(mp_obj_fun_bc_t *self, size_t n_args, size_t n_kw, mp_obj_t *args) {
    MP_DYNRUNTIME_INIT_ENTRY
    mp_store_global(MP_QSTR_decompress, MP_OBJ_FROM_PTR(&mod_decompress_obj));
    mp_store_global(MP_QSTR_compress, MP_OBJ_FROM_PTR(&mod_compress_obj));
    MP_DYNRUNTIME_INIT_EXIT
}
