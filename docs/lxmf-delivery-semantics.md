# LXMF outbound delivery semantics (changed)

Applies to `firmware/urns/lxmf.py` (`LXMRouter.send_message`, `LXMessage`). The scheduling mirrors
reference LXMF 1.3.1 `LXMRouter.process_outbound`; this note is for UI integrations (T-Deck and
others), whose repo lives elsewhere.

## What changed

| | Old | New |
|---|---|---|
| "delivery callback" fires | when the packet was handed to the radio | **only when the receiver's proof arrives** (state DELIVERED) |
| `send_message()` returns | the message, `True` (waiting for a path) or `None` (unknown identity) | the `LXMessage`, or `None` **only when the outbound queue is full** |
| unknown peer / no path | `ensure_path` for 15 s, then a log line | path requests, then the failed callback after the retry sequence |
| failure | a log line | `failed` callback, state FAILED / REJECTED / CANCELLED |

The router-wide **inbound** callback `register_delivery_callback(cb)` is unchanged (a message arrived).

## States and what a UI should show

`LXMessage.state`; `LXMessage.progress` goes 0 to 1 (0.5 once a packet is out, 0.1 to 1.0 over a Resource transfer).

| state | meaning | UI |
|---|---|---|
| `OUTBOUND` (0x01) | queued: waiting for a path, a link, the next attempt, or a held slow path | clock / "queued" |
| `SENT` (0x04) | opportunistic packet transmitted, no proof yet | one tick |
| `SENDING` (0x02) | link packet or Resource in flight, no proof yet | one tick + progress |
| `DELIVERED` (0x08) | proof received | two ticks |
| `FAILED` (0xFF) | retry sequence over, nobody confirmed | "failed", offer retry (a NEW `send_message`) |
| `REJECTED` (0xFD) | the receiver refused a transfer; final, no retry | "refused" |
| `CANCELLED` (0xFE) | `router.cancel_outbound(message.message_id)` | removed |

A message may go SENT -> OUTBOUND -> SENT again (resend / link torn down); only DELIVERED and the failed
states are final.

## Callbacks

Per message: `send_message(..., sent_callback=, delivery_callback=, failed_callback=)` or
`msg.register_sent_callback / register_delivery_callback / register_failed_callback`.
Router-wide: `router.register_outbound_callbacks(sent=, delivered=, failed=)` (also
`register_sent_callback`, `register_delivered_callback`, `register_failed_callback`).
All receive the `LXMessage`. `sent` fires once, at first transmission (what "delivery" used to mean);
`delivered` fires once; `failed` fires for FAILED, REJECTED and CANCELLED.

Queue full: `send_message()` returns `None` and logs a warning. The bound is
`LXMRouter.MAX_PENDING_OUTBOUND` = 8 with plenty of heap (the `const._BIG_RAM` rule), else 4. Nothing is dropped
silently; show "busy, try again".

`msg.message_id` / `msg.hash` stay `None` until the peer's identity is known and the message is built (needed by
`cancel_outbound`). Failed callbacks must tolerate that.

## Adaptive opportunistic wait (deviation)

The reference waits a fixed `max(DELIVERY_RETRY_WAIT 10 s, round trip)` before the next opportunistic attempt. A pure-Python
receiver needs 6-20 s of crypto before it proves, so between two such boards every message was sent and processed twice. Here
the wait is `max(reference wait, 1.5 x the last observed send-to-proof latency of that peer)` (capped at 120 s; latency measured
on the tick clock from the receipt of the proved attempt, kept in a 16-entry per-peer cache), and a node WITHOUT native crypto
(a hint that it lives on a slow-board network) uses reference wait + 15 s (`RECEIPT_PEER_CRYPTO_GRACE`) until a latency is known.
A node with native crypto and no observation behaves exactly as the reference. The attempt count is unchanged. Tests:
`tests/test_review_fixes4.py` (`test_f6_*`).

## Retry sequence (opportunistic, single packet)

Constants: `MAX_DELIVERY_ATTEMPTS 5`, `PROCESSING_INTERVAL 4 s` (the job tick; every time below is a tick),
`DELIVERY_RETRY_WAIT 10 s`, `PATH_REQUEST_WAIT 7 s`, `MAX_PATHLESS_TRIES 1`, `PATH_REQUEST_DEBOUNCE 60 s`,
`SLOW_INTERFACE_BITRATE 2000 bit/s`.

Fast link (TCP/UDP) or SF7/125 kHz LoRa (jitter 0), nobody proves (host tests, `test_lxmf_outbound.py`):

| situation | timeline (seconds after submit) | fails at |
|---|---|---|
| path known, path never returns | send 0, send 12, rediscover path 24 (drop path, request), counted requests 32, 40, 48 | 56 |
| path known, path returns after the rediscovery | send 0, 12, rediscover 24, then sends at the next tick after the path appears, +12 each (28, 40, 52) | 64 |
| no path | request 0 (pre-emptive, not counted), ONE pathless send 8, counted requests 20, 28, 36, 44, 52 | 60 |
| path appears while waiting | sent at the next tick (early trigger), not after the 7 s wait | |
| DIRECT, no path | request 0, 8, 16, 24 | 32 |

SF7 adds up to `mtu_time` (0.73 s) of random jitter per attempt: fail up to 68 s (path known) / 84 s (no path).
300 bit/s link: retry wait 32.7 s, at most 3 attempts, jitter up to 13.3 s: send 0, send ~36-48, request ~72-96, fail
144-180 s (path known), 180-240 s (no path). A slow-path (< ~2 kbit/s next hop) message to a target is held while an
earlier message to it, or any slow-path message, is still in flight.

DIRECT (link) delivery: link established -> `identify` -> packet (or Resource when over ~415 B packed). A link-packet
receipt timeout or a failed Resource tears the link down and returns the message to OUTBOUND (next attempt after the
retry wait); a REJECTED Resource is final. **Deviation:** the reference marks ANY receiver cancel (RCL) of an outgoing resource REJECTED (`Resource._rejected`), including a receiver that cancels after a stall, so a lossy LoRa transfer can fail permanently there. Here an RCL is a rejection only while the resource is still ADVERTISED (no part requested); once the transfer started it is a failed transfer (FAILED) and takes the normal retry path. **The link is closed as soon as nothing queued needs it** (delivered, failed,
cancelled or rejected, and no other pending DIRECT message for the same peer; messages to one peer share a link). This
deviates from the reference, which holds delivery links open for 10 minutes: this port has no return-channel
handling beyond a safety net, so holding the link only costs keepalives. The safety net for the short time the link is
open: an LXMF message that arrives on it IS routed into the normal delivery path. Single packets are delivered once
(dedup by message hash) and really PROVED: links we open carry a real Ed25519 link key when this board has native Ed25519 (`sign_proofs`; without it, a real key costs a blocking ~1.4-2 s pure-Python key generation per link, so the link is opened as before and a message arriving on it is still delivered but NOT proved - the peer resends; the peer
validates the proof against the sig-pub in our link request, as reference `Link.prove_packet` / `validate_proof` do), and
concluded Resources are handed to the same handler as the delivery destination's (the resource layer proves them).
Tests: `tests/test_own_link_proof.py` (real OutgoingLink and real inbound Link back to back). Time to failure for DIRECT depends on the link
establishment timeout and was not measured on hardware.

Link packet receipt timeout: `max(link rtt * 6, 5 s)` (reference: `rtt * 6`, no floor, on a float clock). The link rtt is
measured in milliseconds (`const.ticks_ms`), and receipt ages use the tick clock, so MicroPython's whole-second
`time.time()` can neither shrink a timeout nor fire it early. Opportunistic receipt timeout: first-hop timeout + 6 s per hop (reference) + `RECEIPT_PEER_CRYPTO_GRACE` (15 s, the
receiver's decrypt/sign time: a pure-Python peer needs 6-20 s). Receipts of a message's earlier attempts (the last 3) stay
matchable until the message concludes, so a late proof for attempt 1 still marks it delivered and stops further sends.
An opportunistic send that transmitted nothing (no interface online) is not an attempt and not SENT: no sent callback, no
route rediscovery, retried every tick, failed after `MAX_UNSENT_TICKS` (30) consecutive failures (the reference counts it
as an attempt and shows SENT).

A message that is queued but already concluded no longer
counts against `MAX_PENDING_OUTBOUND`: delivered messages leave the queue when the proof arrives.

## Reference vs port

Same rules; differences: single-threaded (re-entrant `process_outbound` calls are coalesced, not lost), queue bound,
`send_message` takes a destination hash (an unknown identity is requested like a path), only links WE opened are reused
for DIRECT (the reference 1.3.1 also uses peer-opened backchannel links), identify before the first payload.
