# µReticulum

A pure MicroPython implementation of the [Reticulum](https://reticulum.network/) network stack, designed to run on microcontrollers like the ESP32 and Raspberry Pi Pico W.

**Wire-compatible with reference Reticulum** — µReticulum nodes appear as standard peers in MeshChat, Sideband, and NomadNet. Full LXMF messaging support: send and receive encrypted, signed messages with delivery receipts.

## What Works

- **LXMF Messaging** — Send and receive encrypted messages with MeshChat/Sideband, verify Ed25519 signatures, send delivery proofs (receipts). Messages show as "delivered ✓" in MeshChat. Includes an echo bot example that auto-replies to incoming messages.
- **Announce & Discovery** — ESP32 announces itself with an LXMF-compatible display name. Appears in MeshChat's network visualizer. Receives and parses announces from other peers.
- **Full Crypto Stack** — X25519 key exchange, Ed25519 signatures, AES-128-CBC encryption, HKDF, HMAC-SHA256 — all in pure Python, no C extensions.
- **Wire Protocol** — Byte-identical packet format to reference Reticulum. Validated bidirectionally against the reference implementation.
- **UDP Interface** — WiFi networking with auto-detected subnet broadcast. Non-blocking async I/O with ESP32 socket recovery.
- **Serial Interface** — HDLC-framed UART for RNode, LoRa radios, packet radio TNCs, or ESP32-to-ESP32 links.
- **Persistent Identity** — Keys and known destinations survive reboots. JSON configuration.

## Quick Start

### ESP32 / Pico W (MicroPython)

1. Flash MicroPython to your board
2. Copy the `urns/` folder and `example_node.py` to the device
3. Edit WiFi credentials in `example_node.py`:
   ```python
   WIFI_SSID = "YourNetwork"
   WIFI_PASS = "YourPassword"
   NODE_NAME = "ESP32 Node"
   ```
4. Run:
   ```python
   import example_node
   ```

The node will connect to WiFi, announce itself, and begin receiving LXMF messages. It auto-replies with an echo of each message received. Open MeshChat on the same LAN and your ESP32 will appear as a peer.

### Desktop (CPython)

```bash
python example_node.py
```

Runs with the same codebase — useful for testing without hardware.

## Project Structure

```
ureticulum/
├── example_node.py          # Ready-to-run LXMF messaging node
├── test_crypto.py           # 53 tests covering the full crypto stack
├── urns/
│   ├── __init__.py          # Package entry point
│   ├── const.py             # Protocol constants (matching reference RNS)
│   ├── reticulum.py         # Core initialization, config, async event loop
│   ├── identity.py          # Identity management, key generation, announce validation
│   ├── destination.py       # Destination addressing, encryption, announce sending
│   ├── packet.py            # Packet framing, proof generation, receipts
│   ├── transport.py         # Packet routing, announce handling, interface management
│   ├── lxmf.py              # LXMF message format, LXMessage, LXMRouter
│   ├── umsgpack.py          # Minimal MessagePack (subset needed for LXMF)
│   ├── log.py               # Logging with configurable verbosity
│   ├── interfaces/
│   │   ├── __init__.py      # Base Interface class
│   │   ├── udp.py           # WiFi UDP with broadcast discovery
│   │   └── serial.py        # HDLC-framed UART (RNode, LoRa, ESP-to-ESP)
│   └── crypto/
│       ├── x25519.py        # X25519 ECDH key exchange
│       ├── ed25519.py       # Ed25519 signing/verification
│       ├── aes.py           # AES-128-CBC encryption
│       ├── hkdf.py          # HKDF key derivation
│       ├── hmac.py          # HMAC-SHA256
│       ├── hashes.py        # SHA-256 (via hardware when available)
│       ├── sha512.py        # SHA-512 (pure Python for Ed25519)
│       ├── pkcs7.py         # PKCS7 padding
│       ├── token.py         # Fernet-style token encryption
│       └── pure25519/       # Curve25519 field arithmetic
│           ├── _ed25519.py
│           ├── basic.py
│           ├── ed25519_oop.py
│           └── eddsa.py
```

## How It Works

### Message Flow (MeshChat → ESP32)

```
MeshChat                          ESP32 (µReticulum)
   │                                    │
   ├─ LXMF announce ──────────────────► │ Validates Ed25519 signature
   │                                    │ Stores peer identity & display name
   │                                    │
   │ ◄────────────────── LXMF announce ─┤ Sends own announce (+ periodic re-announce)
   │ Peer appears in                    │
   │ network visualizer                 │
   │                                    │
   ├─ Encrypted LXMF message ────────► │ X25519 ECDH decrypt
   │  (opportunistic, single-packet)    │ Unpack msgpack payload
   │                                    │ Verify Ed25519 signature
   │                                    │ Display message content
   │                                    │
   │ ◄──────────────── Delivery proof ──┤ Sign packet hash with Ed25519
   │ Shows "delivered ✓"                │ Send PKT_PROOF back
   │                                    │
   │ ◄────────── Echo reply (LXMF) ────┤ Encrypt + sign reply message
   │ Receives "Echo: ..."              │ Send via opportunistic delivery
   │                                    │
```

### LXMF Wire Format

Each LXMF message on the wire:

| Field | Size | Description |
|-------|------|-------------|
| Destination hash | 16 bytes | Truncated SHA-256 of destination |
| Source hash | 16 bytes | Truncated SHA-256 of source |
| Ed25519 signature | 64 bytes | Signs dest + source + payload + message_id |
| Payload (msgpack) | variable | `[timestamp, title, content, fields]` |

Total overhead: 112 bytes. Content capacity in a single encrypted packet: ~295 bytes.

### Announce App Data (LXMF)

Announces carry msgpack-encoded app data so peers know the node's display name:

```python
# Wire format: msgpack [name_bytes, stamp_cost]
# Example: [b"ESP32 Node", None]
b'\x92\xc4\x0aESP32 Node\xc0'
```

## Performance on ESP32

| Operation | Time |
|-----------|------|
| Receive + decrypt message | ~2s |
| Verify Ed25519 signature | ~2s |
| Sign + send proof | <1s |
| **Total message round-trip** | **~4s** |
| Announce validation | ~6s |
| Free RAM after boot | ~63 KB |
| IDF heap after init | ~34 KB |
| IDF heap during runtime | ~3 KB free (stable) |

The bottleneck is pure-Python Curve25519 arithmetic. For a mesh messaging node on a $4 microcontroller, this is functional for real-world use.

### Memory Management

ESP32's MicroPython uses a split-heap architecture where the Python heap can expand into IDF (C runtime) heap. Crypto operations create large big-integer temporaries that fragment the split heap, permanently consuming IDF memory needed by lwIP for socket receive buffers.

Mitigations:
- **`gc.threshold(4096)`** during boot triggers early GC, reducing fragmentation-driven IDF expansion
- **`_gc_mask` tuning** — crypto loops call `gc.collect()` every N iterations. Boot uses aggressive GC (mask=1, every 2 iters) to prevent IDF heap expansion. After sockets are allocated, runtime switches to relaxed GC (mask=7/15) saving ~4s per message.
- **Pre-importing** `lxmf` and `umsgpack` in `urns/__init__.py` loads bytecode while heap is compact, before crypto key derivation fragments memory
- **Deferred interface setup** — UDP sockets are created after all Python imports, so lwIP gets accurate IDF headroom
- **`gc.threshold(-1)`** at runtime disables the aggressive threshold to avoid ~252 GC calls per Ed25519 verify

## Configuration

The node auto-generates `/rns/config.json` on first boot:

```json
{
  "identity": "<hex-encoded private key>",
  "interfaces": [
    {
      "type": "UDPInterface",
      "name": "WiFi UDP",
      "enabled": true,
      "listen_port": 4242,
      "forward_port": 4242,
      "forward_ip": null
    }
  ]
}
```

Setting `forward_ip` to `null` enables auto-detection of the subnet broadcast address.

### Serial Interface (for RNode / LoRa)

```json
{
  "type": "SerialInterface",
  "name": "Serial Link",
  "enabled": true,
  "uart_id": 2,
  "tx_pin": 17,
  "rx_pin": 16,
  "speed": 115200
}
```

## Testing

```bash
python test_crypto.py
```

Runs 53 tests covering SHA-256, SHA-512, HMAC, HKDF, PKCS7, AES-128-CBC, X25519, Ed25519, Identity, Destination, and Packet. All tests validate against reference Reticulum outputs.

## Compatibility

Tested and confirmed working with:

- **MeshChat** — Bi-directional announces, opportunistic messaging, delivery receipts
- **Reference Reticulum** (Python) — Wire-compatible packets, announces, encryption
- **Reference LXMF** — Cross-validated message packing/unpacking, signature verification

Runs on:

- ESP32 (MicroPython 1.22+)
- Raspberry Pi Pico W (MicroPython)
- Desktop CPython 3.8+ (for development/testing)

## ESP32 Socket Workarounds

The UDP interface includes several workarounds for ESP32 MicroPython lwIP quirks:

- **Separate TX/RX sockets** — Using `sendto()` on a socket registered with `select.poll()` breaks `POLLIN` on ESP32. The TX socket is dedicated to sending and never polled.
- **No `select.poll()`** — `poll(0)` (non-blocking) does not reliably detect incoming UDP data on ESP32. The poll loop uses direct non-blocking `recvfrom()` with `except OSError` instead, matching the pattern used by the serial interface.
- **`setblocking(False)` re-assertion after TX** — Broadcasting via the TX socket on the same port the RX socket is bound to corrupts the RX socket's non-blocking state in lwIP. After every `sendto()`, the RX socket's non-blocking flag is explicitly re-set. Without this, `recvfrom()` silently switches to blocking mode after the first proof/packet send, freezing the entire async event loop.
- **RX socket watchdog** — If the interface previously received traffic but hasn't for 120 seconds, the RX socket is closed and recreated. This catches any remaining socket corruption scenarios.
- **WiFi power management disabled** — `wlan.config(pm=0)` is required to receive broadcast UDP packets on ESP32.

## Limitations

- **Opportunistic delivery only** — Single-packet messages up to ~295 bytes content. Link-based delivery (for larger messages) is not yet implemented.
- **No propagation nodes** — Cannot store-and-forward messages for offline peers.
- **No transport nodes** — Cannot relay packets between interfaces (single-interface only).
- **Pure Python crypto** — ~4 second message round-trip on ESP32. `@micropython.viper` could significantly speed this up.

## What's Next

Potential areas for expansion:

- **Viper-accelerated crypto** — `@micropython.viper` native compilation for field arithmetic could bring X25519 from ~1.4s to ~0.2s
- **Link-based delivery** — Support for messages larger than a single packet
- **RNode integration** — Test with LoRa radio over serial interface
- **Multi-interface routing** — Bridge WiFi ↔ Serial for mesh relay
- **Pico W support** — Verify and tune for RP2040
- **Propagation node** — Store-and-forward for offline peers

## License

MIT

## Acknowledgments

Built on the [Reticulum](https://github.com/markqvist/Reticulum) protocol by Mark Qvist. The pure Python Curve25519 implementation is derived from [pure25519](https://github.com/warner/python-pure25519) by Brian Warner.
