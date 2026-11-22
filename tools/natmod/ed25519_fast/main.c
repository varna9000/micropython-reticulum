/*
 * ed25519_fast — MicroPython native module (.mpy) for Ed25519 + X25519
 *
 * Wraps Monocypher 4.x for ESP32-S3 (xtensawin).
 * Build: make ARCH=xtensawin
 * Upload: mpremote cp ed25519_fast.mpy :ed25519_fast.mpy
 *
 * API:
 *   import ed25519_fast
 *   sig = ed25519_fast.sign(message, seed_32)
 *   ok  = ed25519_fast.verify(sig_64, message, pk_32)
 *   pub = ed25519_fast.publickey(seed_32)
 *   shared = ed25519_fast.x25519(private_32, public_32)
 *   pub = ed25519_fast.x25519_publickey(private_32)
 */

#include "py/dynruntime.h"

/* memcpy/memset/memcmp provided via compat.h (included by all .c files) */
#include "monocypher.h"
#include "monocypher-ed25519.h"

/* Ed25519 sign: sign(message, seed_32) -> bytes(64) */
static mp_obj_t mod_sign(mp_obj_t msg_obj, mp_obj_t sk_seed_obj) {
    mp_buffer_info_t msg_buf, sk_buf;
    mp_get_buffer_raise(msg_obj, &msg_buf, MP_BUFFER_READ);
    mp_get_buffer_raise(sk_seed_obj, &sk_buf, MP_BUFFER_READ);
    if (sk_buf.len != 32) {
        mp_raise_ValueError(MP_ERROR_TEXT("seed must be 32 bytes"));
    }

    uint8_t sk[64], pk[32], sig[64];
    uint8_t seed[32];
    for (int i = 0; i < 32; i++) seed[i] = ((uint8_t *)sk_buf.buf)[i];
    crypto_ed25519_key_pair(sk, pk, seed);
    crypto_wipe(seed, 32);
    crypto_ed25519_sign(sig, sk, msg_buf.buf, msg_buf.len);
    crypto_wipe(sk, 64);

    return mp_obj_new_bytes(sig, 64);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_sign_obj, mod_sign);

/* Ed25519 verify: verify(sig_64, message, pk_32) -> bool */
static mp_obj_t mod_verify(mp_obj_t sig_obj, mp_obj_t msg_obj, mp_obj_t pk_obj) {
    mp_buffer_info_t sig_buf, msg_buf, pk_buf;
    mp_get_buffer_raise(sig_obj, &sig_buf, MP_BUFFER_READ);
    mp_get_buffer_raise(msg_obj, &msg_buf, MP_BUFFER_READ);
    mp_get_buffer_raise(pk_obj, &pk_buf, MP_BUFFER_READ);
    if (sig_buf.len != 64) {
        mp_raise_ValueError(MP_ERROR_TEXT("sig must be 64 bytes"));
    }
    if (pk_buf.len != 32) {
        mp_raise_ValueError(MP_ERROR_TEXT("pk must be 32 bytes"));
    }

    int r = crypto_ed25519_check(sig_buf.buf, pk_buf.buf, msg_buf.buf, msg_buf.len);
    return mp_obj_new_bool(r == 0);
}
static MP_DEFINE_CONST_FUN_OBJ_3(mod_verify_obj, mod_verify);

/* Ed25519 public key: publickey(seed_32) -> bytes(32) */
static mp_obj_t mod_publickey(mp_obj_t sk_seed_obj) {
    mp_buffer_info_t sk_buf;
    mp_get_buffer_raise(sk_seed_obj, &sk_buf, MP_BUFFER_READ);
    if (sk_buf.len != 32) {
        mp_raise_ValueError(MP_ERROR_TEXT("seed must be 32 bytes"));
    }

    uint8_t sk[64], pk[32];
    uint8_t seed[32];
    for (int i = 0; i < 32; i++) seed[i] = ((uint8_t *)sk_buf.buf)[i];
    crypto_ed25519_key_pair(sk, pk, seed);
    crypto_wipe(seed, 32);
    crypto_wipe(sk, 64);

    return mp_obj_new_bytes(pk, 32);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_publickey_obj, mod_publickey);

/* X25519 key exchange: x25519(private_32, public_32) -> bytes(32) */
static mp_obj_t mod_x25519(mp_obj_t sk_obj, mp_obj_t pk_obj) {
    mp_buffer_info_t sk_buf, pk_buf;
    mp_get_buffer_raise(sk_obj, &sk_buf, MP_BUFFER_READ);
    mp_get_buffer_raise(pk_obj, &pk_buf, MP_BUFFER_READ);
    if (sk_buf.len != 32 || pk_buf.len != 32) {
        mp_raise_ValueError(MP_ERROR_TEXT("keys must be 32 bytes"));
    }

    uint8_t shared[32];
    crypto_x25519(shared, sk_buf.buf, pk_buf.buf);

    return mp_obj_new_bytes(shared, 32);
}
static MP_DEFINE_CONST_FUN_OBJ_2(mod_x25519_obj, mod_x25519);

/* X25519 public key: x25519_publickey(private_32) -> bytes(32) */
static mp_obj_t mod_x25519_publickey(mp_obj_t sk_obj) {
    mp_buffer_info_t sk_buf;
    mp_get_buffer_raise(sk_obj, &sk_buf, MP_BUFFER_READ);
    if (sk_buf.len != 32) {
        mp_raise_ValueError(MP_ERROR_TEXT("key must be 32 bytes"));
    }

    uint8_t pk[32];
    crypto_x25519_public_key(pk, sk_buf.buf);

    return mp_obj_new_bytes(pk, 32);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_x25519_publickey_obj, mod_x25519_publickey);

static mp_obj_t mod_stamp2_init(mp_obj_t buf_obj) {
    mp_buffer_info_t buf;
    mp_get_buffer_raise(buf_obj, &buf, MP_BUFFER_WRITE);
    if (((uintptr_t)buf.buf & 3)) { mp_raise_ValueError(MP_ERROR_TEXT("bad argument")); }
    uint32_t n = crypto_x25519_stamp2_init(buf.buf, buf.len);
    if (n == 0) { mp_raise_ValueError(MP_ERROR_TEXT("buffer too small")); }
    return mp_obj_new_int(n);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_stamp2_init_obj, mod_stamp2_init);

static mp_obj_t mod_x25519_stamp2(size_t n_args, const mp_obj_t *args) {
    mp_buffer_info_t seed, mat, tgt, buf;
    mp_get_buffer_raise(args[0], &seed, MP_BUFFER_READ);
    mp_get_buffer_raise(args[1], &mat, MP_BUFFER_READ);
    mp_get_buffer_raise(args[2], &tgt, MP_BUFFER_READ);
    mp_get_buffer_raise(args[4], &buf, MP_BUFFER_WRITE);
    if (seed.len != 32 || tgt.len != 32 || buf.len < 256 || ((uintptr_t)buf.buf & 3)
        || ((uint32_t *)buf.buf)[0] != 0x4b533032u) {
        mp_raise_ValueError(MP_ERROR_TEXT("bad argument"));
    }
    uint8_t prv[32], pub[32];
    uint32_t tried = 0;
    int found = crypto_x25519_stamp2_search(prv, pub, &tried, seed.buf, mat.buf, mat.len,
                                            tgt.buf, (uint32_t)mp_obj_get_int(args[3]), buf.buf,
                                            n_args > 5 ? mp_obj_get_int(args[5]) : 0);
    mp_obj_t items[3];
    items[0] = found ? mp_obj_new_bytes(prv, 32) : mp_const_none;
    items[1] = found ? mp_obj_new_bytes(pub, 32) : mp_const_none;
    items[2] = mp_obj_new_int(tried);
    return mp_obj_new_tuple(3, items);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_x25519_stamp2_obj, 5, 6, mod_x25519_stamp2);

static mp_obj_t mod_hwselftest(mp_obj_t mat_obj, mp_obj_t count_obj) {
    mp_buffer_info_t mat;
    mp_get_buffer_raise(mat_obj, &mat, MP_BUFFER_READ);
    uint32_t words = 0, compared = 0;
    uint32_t bad = crypto_ks_hwselftest(mat.buf, mat.len, (uint32_t)mp_obj_get_int(count_obj), &words, &compared);
    /* (hashes with a mismatch, mismatching words, hashes compared; compared == 0: not run, engine busy) */
    mp_obj_t items[3] = { mp_obj_new_int(bad), mp_obj_new_int(words), mp_obj_new_int(compared) };
    return mp_obj_new_tuple(3, items);
}
/* hwprobe(op): 0 claim the engine -> 1 if owned, 1 -> 1 if our sentinel is still in place, 2 release */
static mp_obj_t mod_hwprobe(mp_obj_t op) {
    return mp_obj_new_int(crypto_ks_hwprobe(mp_obj_get_int(op)));
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_hwprobe_obj, mod_hwprobe);
static MP_DEFINE_CONST_FUN_OBJ_2(mod_hwselftest_obj, mod_hwselftest);
static mp_obj_t mod_hwstats(mp_obj_t reset) {
    uint32_t n[3], mm;
    crypto_ks_hwstats(n, &mm, mp_obj_is_true(reset));
    /* (hardware hashes used, mismatches, times found busy, times taken from us) */
    mp_obj_t items[4] = { mp_obj_new_int(n[0]), mp_obj_new_int(mm), mp_obj_new_int(n[1]), mp_obj_new_int(n[2]) };
    return mp_obj_new_tuple(4, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_hwstats_obj, mod_hwstats);

static mp_obj_t mod_hwstats2(mp_obj_t reset) {
    uint32_t o[7];
    crypto_ks_hwstats2(o, mp_obj_is_true(reset));
    mp_obj_t items[7];
    for (int i = 0; i < 7; i++) { items[i] = mp_obj_new_int(o[i]); }
    return mp_obj_new_tuple(7, items);
}
static MP_DEFINE_CONST_FUN_OBJ_1(mod_hwstats2_obj, mod_hwstats2);

/* hashcash(material, target_32_be, stamp_len, max_tries, seed_32[, hw]) -> (stamp or None, tried)
 * stamp = counter(4 LE) || seed[4:stamp_len]; stamp_len 5..32. */
static mp_obj_t mod_hashcash(size_t n_args, const mp_obj_t *args) {
    mp_buffer_info_t mat, tgt, seed;
    mp_get_buffer_raise(args[0], &mat, MP_BUFFER_READ);
    mp_get_buffer_raise(args[1], &tgt, MP_BUFFER_READ);
    mp_get_buffer_raise(args[4], &seed, MP_BUFFER_READ);
    mp_int_t slen = mp_obj_get_int(args[2]);
    if (tgt.len != 32 || seed.len != 32 || slen < 5 || slen > 32) {
        mp_raise_ValueError(MP_ERROR_TEXT("bad argument"));
    }
    uint8_t stamp[32];
    uint32_t tried = 0;
    int found = crypto_hashcash_search(stamp, (uint32_t)slen, &tried, seed.buf, mat.buf, mat.len,
                                       tgt.buf, (uint32_t)mp_obj_get_int(args[3]),
                                       n_args > 5 ? mp_obj_get_int(args[5]) : 0);
    mp_obj_t items[2];
    items[0] = found ? mp_obj_new_bytes(stamp, (size_t)slen) : mp_const_none;
    items[1] = mp_obj_new_int(tried);
    return mp_obj_new_tuple(2, items);
}
static MP_DEFINE_CONST_FUN_OBJ_VAR_BETWEEN(mod_hashcash_obj, 5, 6, mod_hashcash);

/* Module entry point */
mp_obj_t mpy_init(mp_obj_fun_bc_t *self, size_t n_args, size_t n_kw, mp_obj_t *args) {
    MP_DYNRUNTIME_INIT_ENTRY

    mp_store_global(MP_QSTR_sign, MP_OBJ_FROM_PTR(&mod_sign_obj));
    mp_store_global(MP_QSTR_verify, MP_OBJ_FROM_PTR(&mod_verify_obj));
    mp_store_global(MP_QSTR_publickey, MP_OBJ_FROM_PTR(&mod_publickey_obj));
    mp_store_global(MP_QSTR_x25519, MP_OBJ_FROM_PTR(&mod_x25519_obj));
    mp_store_global(MP_QSTR_x25519_publickey, MP_OBJ_FROM_PTR(&mod_x25519_publickey_obj));
    mp_store_global(MP_QSTR_x25519_stamp2, MP_OBJ_FROM_PTR(&mod_x25519_stamp2_obj));
    mp_store_global(MP_QSTR_x25519_stamp2_init, MP_OBJ_FROM_PTR(&mod_stamp2_init_obj));
    mp_store_global(MP_QSTR_hwselftest, MP_OBJ_FROM_PTR(&mod_hwselftest_obj));
    mp_store_global(MP_QSTR_hwstats, MP_OBJ_FROM_PTR(&mod_hwstats_obj));
    mp_store_global(MP_QSTR_hwprobe, MP_OBJ_FROM_PTR(&mod_hwprobe_obj));
    mp_store_global(MP_QSTR_hwstats2, MP_OBJ_FROM_PTR(&mod_hwstats2_obj));
    mp_store_global(MP_QSTR_hashcash, MP_OBJ_FROM_PTR(&mod_hashcash_obj));

    MP_DYNRUNTIME_INIT_EXIT
}
