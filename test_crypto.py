#!/usr/bin/env python3
"""
µReticulum Crypto Test Suite
Validates all cryptographic primitives against known test vectors
and performs round-trip integration tests.
"""

import sys
import os
import time

# Add project to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

passed = 0
failed = 0

def test(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print("  ✓ " + name)
    else:
        failed += 1
        print("  ✗ FAILED: " + name)


def test_sha256():
    print("\n=== SHA-256 ===")
    from urns.crypto.hashes import sha256
    
    # RFC 6234 test vectors
    h = sha256(b"abc")
    test("SHA-256('abc')", h.hex() == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    
    h = sha256(b"")
    test("SHA-256('')", h.hex() == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    
    h = sha256(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq")
    test("SHA-256(448-bit)", h.hex() == "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1")


def test_sha512():
    print("\n=== SHA-512 ===")
    from urns.crypto.hashes import sha512
    
    h = sha512(b"abc")
    expected = "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f"
    test("SHA-512('abc')", h.hex() == expected)
    
    h = sha512(b"")
    expected = "cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e"
    test("SHA-512('')", h.hex() == expected)


def test_hmac():
    print("\n=== HMAC-SHA256 ===")
    from urns.crypto.hmac import new as hmac_new, digest as hmac_digest
    
    # RFC 4231 Test Case 1
    key = bytes([0x0b] * 20)
    data = b"Hi There"
    h = hmac_new(key, data).digest()
    test("HMAC test case 1", h.hex() == "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7")
    
    # RFC 4231 Test Case 2
    key = b"Jefe"
    data = b"what do ya want for nothing?"
    h = hmac_new(key, data).digest()
    test("HMAC test case 2", h.hex() == "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843")

    # Test standalone digest function
    h2 = hmac_digest(key, data, None)
    test("HMAC standalone digest", h.hex() == h2.hex())


def test_hkdf():
    print("\n=== HKDF ===")
    from urns.crypto.hkdf import hkdf
    
    # RFC 5869 Test Case 1
    ikm = bytes([0x0b] * 22)
    salt = bytes(range(0x00, 0x0d))
    info = bytes(range(0xf0, 0xfa))
    okm = hkdf(length=42, derive_from=ikm, salt=salt, context=info)
    expected = "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
    test("HKDF test case 1", okm.hex() == expected)


def test_pkcs7():
    print("\n=== PKCS7 ===")
    from urns.crypto.pkcs7 import PKCS7
    
    data = b"Hello World!"  # 12 bytes
    padded = PKCS7.pad(data)
    test("PKCS7 pad length", len(padded) == 16)
    test("PKCS7 pad value", padded[-1] == 4)
    
    unpadded = PKCS7.unpad(padded)
    test("PKCS7 round-trip", unpadded == data)
    
    # Full block
    data16 = b"0123456789abcdef"
    padded16 = PKCS7.pad(data16)
    test("PKCS7 full block", len(padded16) == 32 and padded16[-1] == 16)
    test("PKCS7 full block round-trip", PKCS7.unpad(padded16) == data16)


def test_aes():
    print("\n=== AES-256-CBC ===")
    from urns.crypto.aes import AES_256_CBC
    from urns.crypto.pkcs7 import PKCS7
    
    key = os.urandom(32)
    iv = os.urandom(16)
    plaintext = b"This is a test message for AES!"
    
    padded = PKCS7.pad(plaintext)
    ciphertext = AES_256_CBC.encrypt(padded, key, iv)
    decrypted = AES_256_CBC.decrypt(ciphertext, key, iv)
    unpadded = PKCS7.unpad(decrypted)
    
    test("AES-256-CBC round-trip", unpadded == plaintext)
    test("AES-256-CBC ciphertext differs", ciphertext != padded)


def test_token():
    print("\n=== Token ===")
    from urns.crypto.token import Token
    from urns.crypto.aes import AES
    
    key = Token.generate_key()
    token = Token(key, mode=AES)
    
    plaintext = b"Encrypted message via Token"
    ciphertext = token.encrypt(plaintext)
    decrypted = token.decrypt(ciphertext)
    
    test("Token round-trip", decrypted == plaintext)
    test("Token overhead present", len(ciphertext) > len(plaintext))
    
    # Verify HMAC check
    tampered = bytearray(ciphertext)
    tampered[-1] ^= 0xFF
    tampered = bytes(tampered)
    try:
        token.decrypt(tampered)
        test("Token HMAC rejection", False)
    except ValueError:
        test("Token HMAC rejection", True)


def test_x25519():
    print("\n=== X25519 ===")
    from urns.crypto.x25519 import X25519PrivateKey, X25519PublicKey
    
    # Key generation
    prv_a = X25519PrivateKey.generate()
    pub_a = prv_a.public_key()
    test("X25519 key generation", pub_a is not None)
    test("X25519 public key size", len(pub_a.public_bytes()) == 32)
    
    # Key exchange
    prv_b = X25519PrivateKey.generate()
    pub_b = prv_b.public_key()
    
    shared_a = prv_a.exchange(pub_b)
    shared_b = prv_b.exchange(pub_a)
    test("X25519 shared secret match", shared_a == shared_b)
    test("X25519 shared secret size", len(shared_a) == 32)
    
    # Round-trip via bytes
    prv_bytes = prv_a.private_bytes()
    prv_restored = X25519PrivateKey.from_private_bytes(prv_bytes)
    pub_restored = prv_restored.public_key()
    test("X25519 key serialization", pub_restored.public_bytes() == pub_a.public_bytes())
    
    # RFC 7748 test vector
    scalar = bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
    u_coord = bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
    expected = bytes.fromhex("c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552")
    
    from urns.crypto.x25519 import curve25519
    result = curve25519(u_coord, scalar)
    test("X25519 RFC 7748 vector", result == expected)


def test_ed25519():
    print("\n=== Ed25519 ===")
    from urns.crypto.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    
    # Key generation
    prv = Ed25519PrivateKey.generate()
    pub = prv.public_key()
    test("Ed25519 key generation", pub is not None)
    test("Ed25519 public key size", len(pub.public_bytes()) == 32)
    
    # Sign and verify
    message = b"Test message for Ed25519 signature"
    signature = prv.sign(message)
    test("Ed25519 signature size", len(signature) == 64)
    
    try:
        pub.verify(signature, message)
        test("Ed25519 verify valid", True)
    except:
        test("Ed25519 verify valid", False)
    
    # Verify rejection of tampered message
    try:
        pub.verify(signature, b"Tampered message")
        test("Ed25519 reject tampered", False)
    except:
        test("Ed25519 reject tampered", True)
    
    # Verify rejection of tampered signature
    bad_sig = bytearray(signature)
    bad_sig[0] ^= 0xFF
    bad_sig = bytes(bad_sig)
    try:
        pub.verify(bad_sig, message)
        test("Ed25519 reject bad sig", False)
    except:
        test("Ed25519 reject bad sig", True)
    
    # Key serialization round-trip
    prv_bytes = prv.private_bytes()
    prv_restored = Ed25519PrivateKey.from_private_bytes(prv_bytes)
    pub_restored = prv_restored.public_key()
    test("Ed25519 key serialization", pub_restored.public_bytes() == pub.public_bytes())


def test_identity():
    print("\n=== Identity ===")
    from urns.identity import Identity
    
    # Create identity
    ident = Identity()
    test("Identity creation", ident.hash is not None)
    test("Identity hash size", len(ident.hash) == Identity.TRUNCATED_HASHLENGTH // 8)
    
    # Encrypt/decrypt
    plaintext = b"Secret message for identity test"
    ciphertext = ident.encrypt(plaintext)
    decrypted = ident.decrypt(ciphertext)
    test("Identity encrypt/decrypt", decrypted == plaintext)
    
    # Sign/verify
    message = b"Signed message"
    sig = ident.sign(message)
    test("Identity sign", len(sig) == Identity.SIGLENGTH // 8)
    test("Identity verify", ident.validate(sig, message))
    test("Identity reject bad", not ident.validate(sig, b"Wrong message"))
    
    # Serialization
    prv_bytes = ident.get_private_key()
    ident2 = Identity.from_bytes(prv_bytes)
    test("Identity from_bytes", ident2 is not None)
    test("Identity preserved hash", ident2.hash == ident.hash)
    test("Identity preserved encrypt", ident2.decrypt(ciphertext) == plaintext)
    
    # Public key only
    pub_bytes = ident.get_public_key()
    ident3 = Identity(create_keys=False)
    ident3.load_public_key(pub_bytes)
    test("Identity public-only verify", ident3.validate(sig, message))
    test("Identity public-only hash", ident3.hash == ident.hash)


def test_destination():
    print("\n=== Destination ===")
    from urns.identity import Identity
    from urns.destination import Destination
    from urns.transport import Transport
    
    # Reset transport state
    Transport.destinations = []
    
    ident = Identity()
    dest = Destination(ident, Destination.IN, Destination.SINGLE, "test", "app")
    test("Destination creation", dest.hash is not None)
    test("Destination hash size", len(dest.hash) == 16)  # TRUNCATED_HASHLENGTH//8
    test("Destination registered", dest in Transport.destinations)
    
    # Destination hash is deterministic
    hash1 = Destination.hash(ident, "test", "app")
    test("Destination hash deterministic", hash1 == dest.hash)


def test_packet():
    print("\n=== Packet ===")
    from urns.identity import Identity
    from urns.destination import Destination
    from urns.packet import Packet
    from urns.transport import Transport
    
    Transport.destinations = []
    
    ident = Identity()
    dest = Destination(ident, Destination.IN, Destination.SINGLE, "test", "packet")
    
    # Create and pack a PLAIN destination packet
    plain_dest = Destination(None, Destination.IN, Destination.PLAIN, "test", "plain")
    pkt = Packet(plain_dest, b"Hello uReticulum!")
    pkt.pack()
    test("Packet pack", pkt.raw is not None)
    test("Packet size <= MTU", len(pkt.raw) <= 500)
    
    # Unpack
    pkt2 = Packet(destination=None, data=pkt.raw)
    result = pkt2.unpack()
    test("Packet unpack", result == True)
    test("Packet destination hash preserved", pkt2.destination_hash == plain_dest.hash)
    test("Packet data preserved", pkt2.data == b"Hello uReticulum!")
    test("Packet type preserved", pkt2.packet_type == Packet.DATA)


def benchmark_crypto():
    print("\n=== Benchmarks ===")
    from urns.crypto.hashes import sha256, sha512
    from urns.crypto.x25519 import X25519PrivateKey
    from urns.crypto.ed25519 import Ed25519PrivateKey
    
    data = os.urandom(512)
    
    # SHA-256
    start = time.time()
    for _ in range(100):
        sha256(data)
    elapsed = time.time() - start
    print("  SHA-256 (512B × 100): %.3fs (%.1f/s)" % (elapsed, 100/elapsed))
    
    # SHA-512
    start = time.time()
    for _ in range(10):
        sha512(data)
    elapsed = time.time() - start
    print("  SHA-512 (512B × 10):  %.3fs (%.1f/s)" % (elapsed, 10/elapsed))
    
    # X25519 keygen
    start = time.time()
    key = X25519PrivateKey.generate()
    pub = key.public_key()
    elapsed = time.time() - start
    print("  X25519 keygen:        %.3fs" % elapsed)
    
    # X25519 exchange
    key2 = X25519PrivateKey.generate()
    pub2 = key2.public_key()
    start = time.time()
    key.exchange(pub2)
    elapsed = time.time() - start
    print("  X25519 exchange:      %.3fs" % elapsed)
    
    # Ed25519 keygen
    start = time.time()
    sk = Ed25519PrivateKey.generate()
    elapsed = time.time() - start
    print("  Ed25519 keygen:       %.3fs" % elapsed)
    
    # Ed25519 sign
    msg = b"Benchmark message"
    start = time.time()
    sig = sk.sign(msg)
    elapsed = time.time() - start
    print("  Ed25519 sign:         %.3fs" % elapsed)
    
    # Ed25519 verify
    vk = sk.public_key()
    start = time.time()
    vk.verify(sig, msg)
    elapsed = time.time() - start
    print("  Ed25519 verify:       %.3fs" % elapsed)


if __name__ == "__main__":
    print("=" * 50)
    print("µReticulum Crypto Test Suite")
    print("=" * 50)
    
    start_time = time.time()
    
    test_sha256()
    test_sha512()
    test_hmac()
    test_hkdf()
    test_pkcs7()
    test_aes()
    test_token()
    test_x25519()
    test_ed25519()
    test_identity()
    test_destination()
    test_packet()
    benchmark_crypto()
    
    total = time.time() - start_time
    print("\n" + "=" * 50)
    print("Results: %d passed, %d failed (%.2fs)" % (passed, failed, total))
    print("=" * 50)
    
    if failed > 0:
        sys.exit(1)
