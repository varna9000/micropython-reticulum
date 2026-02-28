# µReticulum

A pure MicroPython implementation of the [Reticulum](https://reticulum.network/) network stack for ESP32 microcontrollers.

**Wire-compatible with reference Reticulum** — µReticulum nodes appear as standard peers in MeshChat, Sideband, and NomadNet. Full LXMF messaging support: send and receive encrypted, signed messages with delivery receipts.

## Target Hardware

Developed and tested on the [Waveshare ESP32-S3-Zero](https://www.waveshare.com/wiki/ESP32-S3-Zero) — a compact ($4) board with:

- ESP32-S3 dual-core LX7 @ 240MHz
- 4MB Flash, 2MB PSRAM, 512KB SRAM
- 2.4GHz WiFi (802.11 b/g/n) + Bluetooth 5 (LE)
- Onboard WS2812 NeoPixel LED (GPIO 21)
- USB-C, castellated pads, 24.8mm x 18mm

Requires MicroPython 1.22+. Should also work on other ESP32-S3 boards and Raspberry Pi Pico W.

## What Works

- **LXMF Messaging** — Send and receive encrypted messages with MeshChat/Sideband, verify Ed25519 signatures, send delivery proofs (receipts). Messages show as "delivered" in MeshChat. Includes an echo bot example that auto-replies to incoming messages.
- **NeoPixel Control via LXMF** — Send color commands (`red`, `green`, `blue`, `off`) from MeshChat to control the onboard LED. Demonstrates using Reticulum as a hardware control channel.
- **Announce & Discovery** — ESP32 announces itself with an LXMF-compatible display name. Appears in MeshChat's network visualizer. Receives and parses announces from other peers.
- **Full Crypto Stack** — X25519 key exchange, Ed25519 signatures, AES-128-CBC encryption, HKDF, HMAC-SHA256 — all in pure Python, no C extensions.
- **Wire Protocol** — Byte-identical packet format to reference Reticulum. Validated bidirectionally against the reference implementation.
- **UDP Interface** — WiFi networking with auto-detected subnet broadcast. Non-blocking async I/O with ESP32 socket recovery.
- **Serial Interface** — HDLC-framed UART for RNode, LoRa radios, packet radio TNCs, or ESP32-to-ESP32 links.
- **Persistent Identity** — Keys and known destinations survive reboots. JSON configuration.

## Quick Start

1. Flash MicroPython to your ESP32-S3-Zero
2. Copy the `urns/` folder and `example_node.py` to the device
3. Edit WiFi credentials in `example_node.py`:
   ```python
   WIFI_SSID = "YourNetwork"
   WIFI_PASS = "YourPassword"
   NODE_NAME = "ESP32s3"
   ```
4. Run:
   ```python
   import example_node
   ```

The node will connect to WiFi, announce itself, and begin receiving LXMF messages. It auto-replies with an echo of each message received. Open MeshChat on the same LAN and your ESP32 will appear as a peer.

### NeoPixel Control

The example node controls the onboard WS2812 NeoPixel LED via LXMF messages. Send any of these commands from MeshChat/Sideband:

| Message | Effect |
|---------|--------|
| `red` | LED turns red |
| `green` | LED turns green |
| `blue` | LED turns blue |
| `off` | LED turns off |

Commands are case-insensitive. Any other message is echoed back as a reply. This demonstrates using Reticulum as a control channel for IoT hardware — the same pattern works for relays, sensors, motors, or any GPIO-connected device.

## Project Structure

```
ureticulum/
├── example_node.py          # LXMF messaging node with NeoPixel control
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
│       ├── aes.py           # AES-128/256-CBC encryption (via ucryptolib)
│       ├── hkdf.py          # HKDF key derivation
│       ├── hmac.py          # HMAC-SHA256
│       ├── hashes.py        # SHA-256 (via uhashlib), SHA-512 (pure Python)
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
MeshChat                          ESP32-S3 (µReticulum)
   │                                    │
   ├─ LXMF announce ──────────────────► │ Validates Ed25519 signature
   │                                    │ Stores peer identity & display name
   │                                    │
   │ ◄────────────────── LXMF announce ─┤ Sends own announce (+ periodic re-announce)
   │ Peer appears in                    │
   │ network visualizer                 │
   │                                    │
   ├─ Encrypted LXMF message ────────► │ X25519 ECDH decrypt
   │  (e.g. "green")                    │ Unpack msgpack payload
   │                                    │ Verify Ed25519 signature
   │                                    │ Set NeoPixel color / echo reply
   │                                    │
   │ ◄──────────────── Delivery proof ──┤ Sign packet hash with Ed25519
   │ Shows "delivered"                  │ Send PKT_PROOF back
   │                                    │
   │ ◄────────── Echo reply (LXMF) ────┤ Encrypt + sign reply message
   │ Receives "Echo: green"            │ Send via opportunistic delivery
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
# Example: [b"ESP32s3", None]
b'\x92\xc4\x07ESP32s3\xc0'
```

## Performance on ESP32-S3

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

## Compatibility

Tested and confirmed working with:

- **MeshChat** — Bi-directional announces, opportunistic messaging, delivery receipts
- **Reference Reticulum** (Python) — Wire-compatible packets, announces, encryption
- **Reference LXMF** — Cross-validated message packing/unpacking, signature verification

## ESP32 Socket Workarounds

The UDP interface includes several workarounds for ESP32 MicroPython lwIP quirks:

- **Single TX/RX socket** — saves ~280 bytes IDF heap vs two sockets
- **`settimeout(0)` re-asserted after every `sendto()`** — ESP32 lwIP bug: `sendto()` corrupts the socket's non-blocking state. Without this, `recvfrom()` silently blocks after the first send, freezing the async event loop.
- **No `select.poll()`** — `poll(0)` doesn't reliably detect incoming UDP on ESP32 lwIP. Uses direct non-blocking `recvfrom()` + `except OSError` instead.
- **RX socket watchdog** — If the interface previously received traffic but hasn't for 60 seconds, the socket is closed and recreated.
- **WiFi power management disabled** — `wlan.config(pm=0)` is required to receive broadcast UDP packets.
- **AP_IF deactivated** — dual-interface mode routes broadcast packets to AP instead of STA, preventing UDP broadcast reception.

## Limitations

- **MicroPython only** — no CPython/desktop support. Uses `uhashlib`, `ucryptolib`, `uasyncio`, `micropython.const` directly.
- **Opportunistic delivery only** — Single-packet messages up to ~295 bytes content. Link-based delivery (for larger messages) is not yet implemented.
- **No propagation nodes** — Cannot store-and-forward messages for offline peers.
- **No transport nodes** — Cannot relay packets between interfaces (single-interface only).
- **Pure Python crypto** — ~4 second message round-trip on ESP32. `@micropython.viper` could significantly speed this up.

## What's Next

Potential areas for expansion:

- **Viper-accelerated crypto** — `@micropython.viper` native compilation for field arithmetic could bring X25519 from ~1.4s to ~0.2s
- **Link-based delivery** — Support for messages larger than a single packet
- **RNode integration** — Test with LoRa radio over serial interface
- **Multi-interface routing** — Bridge WiFi <-> Serial for mesh relay
- **More hardware control** — Expand NeoPixel example to sensors, relays, displays
- **Propagation node** — Store-and-forward for offline peers

## License

MIT

## Acknowledgments

Built on the [Reticulum](https://github.com/markqvist/Reticulum) protocol by Mark Qvist. The pure Python Curve25519 implementation is derived from [pure25519](https://github.com/warner/python-pure25519) by Brian Warner.
