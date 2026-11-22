# Protocol details

Deep-dive reference for µReticulum: message and link flows, wire formats, the hardening added for 1.3.9 / 1.4.x parity, the LoRa split-packet protocol, ESP32 socket workarounds, the native crypto and BZ2 modules, and which reference-RNS releases changed what. You do not need any of it to use the project; it is here for anyone curious how the port interoperates with reference Reticulum. Back to the [README](../README.md).

## Reference RNS version tracking

Protocol behaviour tracks **reference RNS 1.6.x** (1.6 adds proof-of-work network stamps, which do affect the wire; see *Network stamps* in the [README](../README.md#network-stamps-reticulum-16)). The 1.3.9 to 1.5.2 notes below still apply. The 1.3.9 link and resource
safeguards are implemented here (see the *Resource and link safeguards* block
under [Resource and link safeguards](#resource-and-link-safeguards-parity-with-rns-139)), as is the whole of 1.4.x that
applies to a leaf or relay node: dynamic link path re-balancing, interface
gravity, RTT-scaled keepalive and stale windows with the keepalive-reply
throttle, `max_request_size` / `max_response_size`, and out-of-window rejection
on `Channel` (see [Link path re-balancing and keepalives](#link-path-re-balancing-and-keepalives-parity-with-rns-14x)). Neither 1.4.x nor 1.5.x
changed the wire format, so older and newer peers interoperate either way.

RNS 1.5.0's headline is a priority-based inbound ingress-queue rewrite of
`Transport`, built on OS threads and locks — there is no analogue for this
single-threaded `uasyncio` port, and none is needed for interop: the port
already prioritises inline data, proof and link traffic over deferred announce
validation. The two items from that release that apply to a leaf or relay node
are implemented: an excessive-hop-count drop (`PATHFINDER_M = 128`, rejected in
`Transport.packet_filter`) and a constant-time HMAC comparison in
`Token.verify_hmac`. Identity blackholing is supported (`Transport.blackhole()`);
the operator blackhole publish/subscribe lists and everything surfaced only
through `rnstatus` are out of scope or opt-in, and none of it affects
interoperability.

RNS 1.5.1 and 1.5.2 likewise changed no wire format. Both are dominated by work
with no analogue on a single-threaded MCU — adaptive dataplane ingress/egress
control layered on the 1.5.0 queue rewrite, `BackboneInterface` transmit
buffers, live profiling, and `rnstatus` diagnostics — or by fixes already
covered here (the 1.5.2 resource-cancel guard cannot occur, since cancellation
routes through `resource_concluded` and the link's `cancel_*_resource` already
check membership; bz2 compression already falls back to uncompressed). The two
frame-validation hardening checks that apply to a leaf or relay node are
implemented: `Packet.unpack` rejects a zero-length data field, and
`Transport.packet_filter` drops an announce frame larger than the MTU.

Out of scope for an MCU port: `BackboneInterface` flap-blocking, interface
discovery, I2P, shared-instance/tunnel interfaces, and the `rnsh` utility.

> **If you run an `rnsh` listener** (any platform), update it to RNS 1.3.9: that
> release patches a critical vulnerability where a command could be started on a
> session that never completed identity authorisation. This port contains no
> listener, so it is not affected — but a listener elsewhere on your mesh is.

## Message flow (MeshChat → ESP32-S3)

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
```

## LXMF wire format

| Field | Size | Description |
|-------|------|-------------|
| Destination hash | 16 bytes | Truncated SHA-256 of destination |
| Source hash | 16 bytes | Truncated SHA-256 of source |
| Ed25519 signature | 64 bytes | Signs dest + source + payload + message_id |
| Payload (msgpack) | variable | `[timestamp, title, content, fields]` |

Total overhead: 112 bytes. Content capacity in a single encrypted packet: ~295 bytes.

Announces carry msgpack-encoded app data so peers know the node's display name:

```python
# Wire format: msgpack [name_bytes, stamp_cost]
# Example: [b"ESP32s3", None]
b'\x92\xc4\x07ESP32s3\xc0'
```

## NomadNet link handshake

```
NomadNet Client                     ESP32-S3 (µReticulum)
   │                                       │
   ├─ Link Request (X25519 pub key) ─────► │ Generate ephemeral X25519 keypair
   │                                       │ ECDH shared secret → HKDF → AES-256 Token
   │                                       │
   │ ◄───── Link Proof (signature + pub) ──┤ Sign with destination Ed25519 identity
   │                                       │
   ├─ RTT (encrypted) ──────────────────► │ Link ACTIVE
   │                                       │
   ├─ Page Request (encrypted RPC) ──────► │ Decrypt, look up handler by path hash
   │                                       │ Read .mu file, substitute variables
   │ ◄──────── Page Response (encrypted) ──┤ Encrypt and send
```

Each link consumes ~350 bytes of RAM. Up to 4 concurrent links are supported (`MAX_ACTIVE_LINKS=4`). Idle links are cleaned up after 12 minutes.

## Outbound link establishment on half-duplex LoRa (LRRTT resend + delivery retry)

Reference RNS marks a responder link *established* — and starts accepting resource
transfers on it — **only** when the initiator's RTT packet (LRRTT) arrives. That
packet is sent exactly once, and on a half-duplex LoRa mesh it is unusually easy to
lose: a node typically opens its reply link seconds after proving an inbound
message, while the relay upstream is still transmitting the sibling link's traffic —
its radio is deaf mid-TX and the LRRTT dies. The failure is silent and deceptive:
the half-established peer still answers keepalives (raw 1-byte frames, processed
regardless of link state) but discards every resource advertisement, so the link
looks alive while nothing ever delivers.

µReticulum hardens both layers on the initiator side:

- **LRRTT resend** (`link.py`) — the RTT payload is kept and re-sent (up to 4×,
  every 4 s) until the peer sends *anything that decrypts*, which proves its side
  of the link completed establishment (each resend re-encrypts with a fresh IV, so
  transport dedup never drops it; a peer that was already established just re-fires
  its idempotent established-callback).
- **DIRECT delivery retry** (`lxmf.py`) — if the link dies before the message got
  through (establishment timeout: lost LR or proof) or the resource transfer fails,
  the delivery is re-attempted on a fresh link, 3 attempts total — matching
  reference LXMF's retry behavior.

## Resource and link safeguards (parity with RNS 1.3.9)

A link peer is untrusted input: it can send a malformed resource advertisement,
re-identify mid-session, or vanish mid-transfer. Reference RNS 1.3.9 tightened
these paths, and this port implements the equivalents — with an MCU's much
smaller margin for error in mind.

- **Advertisement validation** — every advertisement is parsed *and* validated
  inside one guarded block: msgpack errors, missing fields, wrong types,
  negative or absurd sizes are rejected before anything is allocated. On a
  desktop an unchecked size claim wastes memory; on an ESP32 it is an immediate
  out-of-memory. An advertisement that cannot be processed at all tears the link
  down rather than leaving it looping on bad input.
- **Pre-send link check** — a resource verifies its link is still `ACTIVE`
  before every advertisement, part, request and proof. A closing link nulls its
  resources' references, so an unguarded watchdog send would raise inside the
  event loop.
- **Cancellation signalling** — cancelling tells the peer: `RESOURCE_ICL` from
  the sender, `RESOURCE_RCL` from the receiver (the receiver-side signal is new
  in 1.3.9). Without it the far end keeps re-advertising or re-requesting until
  its own timeout — minutes of wasted airtime on half-duplex LoRa. A cancel that
  arrives *from* the peer is never echoed back.
- **Identity binds once** — a second `identify` on an established link is
  ignored, so anything authorising on the identified identity cannot have its
  authorisation subject swapped mid-session.
- **HDLC frame validation** (TCP interface) — frames shorter than a packet
  header, or that overflowed the buffer mid-flight, are dropped instead of
  handed to routing as truncated packets.

Covered by `firmware/tests/test_resource_safeguards.py`.

## Link path re-balancing and keepalives (parity with RNS 1.4.x)

A link request and the proof that answers it do not always travel the same
number of hops — a route can shorten or lengthen between the two, and on a mesh
with several possible paths they can simply differ. Both ends check the proof's
hop count, so a mismatch used to mean the link never came up at all.

- **Re-balancing at a relay** — when a transit link-request proof arrives with a
  hop count other than the one recorded for that link, the relay verifies the
  proof signature and then adopts the new count, in both the link table and the
  path table, instead of dropping the proof. The signature check is mandatory
  here: this rewrites routing state, so an unverifiable proof (no native
  Ed25519, or `strict_lr_validation` off) is still dropped — a failed link beats
  an unauthenticated hop rewrite.
- **Re-balancing at the initiator** — the same correction is applied to the path
  table once our own link goes active. No extra crypto is spent: the link only
  reaches that state after the peer's signature over our link id has been
  verified.
- **Keepalive on outbound silence** — what stales a link at the far end is how
  long since *we* transmitted, not how long since we heard. An initiator that
  only receives (a peer streaming to it) used to fall silent and get torn down
  mid-stream; it now probes when either direction has been quiet.
- **RTT-scaled windows** — the RTT the initiator measures is carried in the
  handshake and now sizes the receiver's keepalive and stale windows
  (`clamp(rtt × 360/1.75, 5, 360)`, stale = twice that). LoRa clamps back to the
  360 s / 720 s pair this port used unconditionally before; a fast link drops to
  roughly 20 s / 41 s, so a dead TCP link is reaped in under a minute instead of
  twelve. An absent or unusable value keeps the old defaults.
- **Keepalive reply throttle** — with both ends deriving that window from the
  same RTT, a `0xFF` probe is answered only if we have been quiet for it.
  Anything transmitted inside the window already proved us alive, and on
  half-duplex LoRa the saved frame is one that would have gone out exactly when
  the channel is busiest.
- **Channel window** — a message sequence past the far edge of the receive
  window is rejected rather than buffered forever behind a gap that can never be
  filled.

**Request and response size limits** — `destination.set_max_request_size(n)`
caps what a destination's request handlers will accept, and
`link.request(..., max_response_size=n)` caps what comes back. Both refuse
before buffering: an oversized single-packet request is dropped before it is
unpacked, and an oversized resource is cancelled with an `RCL` before a single
part transfers, so the sender stops immediately rather than retrying for
minutes. Without a limit the stack's own 16 KB `MAX_RESOURCE_SIZE` ceiling still
applies — worth lowering on a node with tens of KB of free heap, especially over
TCP where the negotiated link MTU reaches 16 KB.

Covered by `firmware/tests/test_transport.py`, `test_link_request.py`,
`test_resource_safeguards.py` and `test_channel.py`, and verified on an ESP32-S3
(T-Deck) against real native Ed25519.

## SX1262 LoRa — RNode split-packet protocol

The LoRa interface implements the same split-packet framing as [RNode firmware](https://github.com/markqvist/RNode_Firmware), enabling transparent interop with RNode devices and support for Reticulum's full 500-byte MTU over LoRa's 255-byte frame limit.

Every LoRa frame carries a **1-byte RNode header**:

| Bits | Field | Description |
|------|-------|-------------|
| 7–4 | Sequence | Random 4-bit value for matching split halves |
| 0 | FLAG_SPLIT | Set when packet is split across 2 frames |

- **Single frame** (data ≤ 254 bytes): `[header] [data]` — max 255 bytes
- **Split packet** (data 255–508 bytes): two frames with the same header byte (same sequence + FLAG_SPLIT), back-to-back:
  - Frame 1: `[header] [first 254 bytes]` = 255 bytes
  - Frame 2: `[header] [remaining bytes]`

The receiver matches split frames by sequence number and reassembles them into a complete Reticulum packet. Stale fragments are discarded after 15 seconds.

The `lora-sx126x` MicroPython driver sends and receives bytes faithfully — the RNode header byte is the first byte returned by `poll_recv()` on RX and the first byte written by `send()` on TX. No FIFO offset workarounds are needed.

Packets with bit 7 set in the Reticulum flags byte (IFAC-tagged) are validated if IFAC is configured on the receiving interface, or dropped if IFAC is not configured. This matches reference Reticulum behavior.

## ESP32-S3 socket workarounds

The UDP interface includes several workarounds for ESP32-S3 MicroPython lwIP quirks:

- **Single TX/RX socket** — saves ~280 bytes IDF heap vs two sockets.
- **`settimeout(0)` re-asserted after every `sendto()`** — ESP32-S3 lwIP bug: `sendto()` corrupts the socket's non-blocking state. Without this, `recvfrom()` silently blocks after the first send, freezing the async event loop.
- **No `select.poll()`** — `poll(0)` doesn't reliably detect incoming UDP on ESP32-S3 lwIP. Uses direct non-blocking `recvfrom()` + `except OSError` instead.
- **RX socket watchdog** — if the interface previously received traffic but hasn't for 60 seconds, the socket is closed and recreated.
- **WiFi power management disabled** — `wlan.config(pm=0)` is required to receive broadcast UDP packets (with `WIFI_POWER_SAVE = "auto"` this applies only while an enabled `UDPInterface` is configured; other setups keep modem power saving on).
- **AP_IF deactivated** — dual-interface mode routes broadcast packets to AP instead of STA, preventing UDP broadcast reception.

## Native crypto module (.mpy details)

The native C module wraps [Monocypher](https://monocypher.org/) compiled to native machine code, distributed as a `.mpy` file alongside the Python code (no firmware recompile needed).

| Operation | Pure Python | Native C | Speedup |
|-----------|-------------|----------|---------|
| Ed25519 sign | 2 000 ms | 12 ms | **166×** |
| Ed25519 verify | 2 000 ms | 18 ms | **111×** |
| X25519 exchange | 1 400 ms | 13 ms | **107×** |

`.mpy` files are version 6 (MicroPython 1.19+) and architecture-specific:

| File | Architecture | Devices |
|------|--------------|---------|
| `ed25519_fast_xtensawin.mpy` | Xtensa (windowed) | ESP32-S3 |
| `ed25519_fast_armv6m.mpy` | ARM Cortex-M0+ | RP2040 (Pico W) |

On ESP32-S3 the same build is also shipped as `ed25519_iram.mpy` (identical content; the loader tries that name first and falls back to `ed25519_fast_xtensawin.mpy`). Both ship pre-built in `firmware/lib/` and are loaded automatically. If a future MicroPython version changes the `.mpy` format, see [BUILDING_NATIVE_MODULES.md](../tools/natmod/BUILDING_NATIVE_MODULES.md) for cross-compilation instructions.

## Native BZ2 module

Reference RNS always compresses Resource transfers with bz2. The native C module provides **both compression and decompression**, producing stdlib-compatible bz2 output that interoperates with reference RNS.

- **Decompression**: ~100× faster than pure Python (~2 ms vs ~200 ms for 1 KB). Falls back to pure Python if native module is missing.
- **Compression**: ~500 ms for 1 KB on ESP32-S3. Reduces text payloads by 60–80% (e.g. 1 253 B → 394 B). Only available with native module — without it, Resources are sent uncompressed (which is valid).

| File | Architecture | Devices |
|------|--------------|---------|
| `bz2_fast_xtensawin.mpy` | Xtensa (windowed) | ESP32-S3 |
| `bz2_fast_armv6m.mpy` | ARM Cortex-M0+ | RP2040 (Pico W) |
