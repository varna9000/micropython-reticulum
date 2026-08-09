# µReticulum Resource Transfer
# Wire-compatible with reference RNS Resource protocol
# Supports segmented data transfer over Links for payloads > single packet

import time
from . import const, umsgpack
from .log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE
from .identity import Identity
from .crypto.hashes import sha256

# Millisecond monotonic clock for round timing. Device time.time() is whole
# seconds — dividing rate math by it is a ZeroDivisionError on any fast round.
# Same host-CPython fallback pattern as transport.py.
_ticks_ms = getattr(time, "ticks_ms", None) or (lambda: int(time.time() * 1000))
_ticks_diff = getattr(time, "ticks_diff", None) or (lambda a, b: a - b)


# Constants (wire-compatible with reference RNS)
MAPHASH_LEN = 4
RANDOM_HASH_SIZE = 4
HASHMAP_IS_EXHAUSTED = 0xFF
HASHMAP_IS_NOT_EXHAUSTED = 0x00
MAX_RESOURCE_SIZE = 16384  # 16KB — ESP32 memory safe

# Windowing (reference RNS Resource parity). The receiver requests up to
# `window` parts per round; the window grows each completed round and its cap
# unlocks (or clamps) with the measured transfer rate — the same code ramps a
# TCP link to big windows while LoRa settles at WINDOW_MAX_VERY_SLOW.
WINDOW               = 4
WINDOW_MIN           = 2
WINDOW_MAX_SLOW      = 10
WINDOW_MAX_VERY_SLOW = 4       # sustained < 2 kbps: LoRa lands here
WINDOW_MAX_FAST      = 75      # request pkt 1+32+75*4 = 333 B fits mdu 431;
                               # in-flight RAM is bounded by MAX_RESOURCE_SIZE
FAST_RATE_THRESHOLD  = 4       # samples above RATE_FAST to unlock FAST
VERY_SLOW_RATE_THRESHOLD = 2
RATE_FAST            = 6250.0  # B/s (50 kbps)
RATE_VERY_SLOW       = 250.0   # B/s (2 kbps)
WINDOW_FLEXIBILITY   = 4

# Receiver part-timeout scaling (reference PART_TIMEOUT_FACTOR*). Timers
# derive from the measured in-flight rate (eifr) or the link RTT — no fixed
# interval fits both a ~50 ms TCP round and a ~28 s SF11 round.
PART_TIMEOUT_FACTOR           = 4      # before a rate is measured
PART_TIMEOUT_FACTOR_AFTER_RTT = 2
RETRY_GRACE_TIME = 0.25
PER_RETRY_DELAY  = 0.5
T_PART_MIN = 1.0     # floor: job_loop services us at 0.25 s ticks
T_PART_MAX = 60.0    # cap: SF11 initial rounds
MAX_RETRIES     = 16   # receiver retry budget — spent on timeouts only,
                       # refunded by progress (any received part)
MAX_ADV_RETRIES = 4
TIMEOUT = 120          # overall wall-clock ceiling (16 KB cap bounds it)

# Rate samples approximate wire bytes (reference samples len(packet.raw)).
# Token overhead applies to the request packet; parts are pre-encrypted
# stream slices, so only the header rides on top.
_WIRE_OVERHEAD_REQ  = const.MTU - const.ENCRYPTED_MDU
_WIRE_OVERHEAD_PART = const.HEADER_MINSIZE

# Resource flags
FLAG_ENCRYPTED = 0x01
FLAG_COMPRESSED = 0x02
FLAG_IS_RESPONSE = 0x10

# States
# Link.ACTIVE / OutgoingLink.ACTIVE — both link classes use the same value.
_LINK_ACTIVE = 0x01

NONE = 0x00
ADVERTISED = 0x01
TRANSFERRING = 0x02
AWAITING_PROOF = 0x03
ASSEMBLING = 0x04
COMPLETE = 0x05
FAILED = 0x06
CORRUPT = 0x07


class Resource:
    """Segmented data transfer over a Link.

    Sender mode: Resource(link, data, is_response=True, request_id=...)
    Receiver mode: Resource.accept(adv_data, link)
    """

    def __init__(self, link, data, is_response=False, request_id=None):
        """Create a sender-side Resource. Encrypts, splits, and advertises."""
        import gc

        if len(data) > MAX_RESOURCE_SIZE:
            raise ValueError("Resource too large: " + str(len(data)))

        self.link = link
        self.status = NONE
        self.is_initiator = True
        self.request_id = request_id
        self.created_at = time.time()
        self.data = data
        self.total_data_size = len(data)
        self.retries = 0
        self.adv_retries = 0
        self.last_adv_at = 0
        self.rtt = None   # measured when the first part request answers the adv

        # Generate random hash
        self.random_hash = Identity.get_random_hash()[:RANDOM_HASH_SIZE]

        # Compute resource hash and expected proof from plaintext
        self.hash = Identity.full_hash(data + self.random_hash)
        self.expected_proof = Identity.full_hash(data + self.hash)

        # Try bz2 compression (requires native C module)
        self.compressed = False
        try:
            from .bz2dec import compress as bz2_compress
            compressed = bz2_compress(data)
            if compressed and len(compressed) < len(data):
                data = compressed
                self.compressed = True
                log("Resource compressed " + self.hash.hex()[:8] + ": " + str(self.total_data_size) + "B -> " + str(len(data)) + "B", LOG_DEBUG)
            if compressed:
                del compressed
        except Exception:
            pass

        # Encrypt: random_hash + data with link token
        gc.collect()
        plaintext = self.random_hash + data
        self.encrypted = self.link._token.encrypt(plaintext)
        del plaintext
        gc.collect()

        # Compute part size (same as reference: Packet.MDU)
        self.sdu = self.link.sdu

        # Split into parts
        self.parts = []
        offset = 0
        while offset < len(self.encrypted):
            end = min(offset + self.sdu, len(self.encrypted))
            self.parts.append(self.encrypted[offset:end])
            offset = end
        self.total_parts = len(self.parts)

        # Compute hashmap
        self.hashmap = b""
        for part in self.parts:
            self.hashmap += Identity.full_hash(part + self.random_hash)[:MAPHASH_LEN]

        # Build flags
        self.flags = FLAG_ENCRYPTED
        if self.compressed:
            self.flags |= FLAG_COMPRESSED
        if is_response:
            self.flags |= FLAG_IS_RESPONSE

        self.sent_count = 0
        # Per-part "have we ever served this part?" tracker so the progress
        # log reflects unique parts delivered, not raw TX events including
        # retransmits.
        self._parts_served = [False] * self.total_parts
        self._unique_parts_served = 0

        # Register with link
        self.link.register_outgoing_resource(self)

        log("Resource created: " + str(len(data)) + "B -> " +
            str(self.total_parts) + " parts, hash=" + self.hash.hex()[:8], LOG_VERBOSE)

        # Free original data — we have encrypted form
        self.data = data  # Keep for proof verification
        gc.collect()

        # Advertise
        self.advertise()

    @staticmethod
    def accept(adv_data, link):
        """Create a receiver-side Resource from an advertisement."""
        import gc
        gc.collect()

        r = object.__new__(Resource)
        r.link = link
        r.is_initiator = False
        r.created_at = time.time()
        r.retries = 0
        r.data = None

        # Parse AND validate in one guarded block. A malformed or hostile
        # advertisement — bad msgpack, missing fields, wrong types, absurd sizes
        # — must never raise into the link receive path and must never drive an
        # allocation: on an MCU an unchecked size claim is an immediate
        # out-of-memory. Reference RNS 1.3.9 added equivalent safeguards
        # (typed unpack limits + a transfer-size ceiling).
        try:
            adv = umsgpack.unpackb(adv_data)
            r.total_size = adv["t"]       # encrypted (transfer) size
            r.total_data_size = adv["d"]  # original data size
            r.total_parts = adv["n"]
            r.hash = adv["h"]
            r.random_hash = adv["r"]
            r.original_hash = adv["o"]
            r.segment_index = adv["i"]
            r.total_segments = adv["l"]
            r.request_id = adv["q"]
            r.flags = adv["f"]
            hashmap_raw = adv["m"]
            if not (isinstance(r.total_size, int) and isinstance(r.total_data_size, int)
                    and isinstance(r.total_parts, int) and isinstance(r.segment_index, int)
                    and isinstance(r.total_segments, int)):
                raise ValueError("non-integer size field")
            if not (isinstance(r.hash, bytes) and isinstance(hashmap_raw, bytes)):
                raise ValueError("non-binary hash field")
            if r.total_size < 0 or r.total_data_size < 0 or r.total_parts < 0:
                raise ValueError("negative size")
            if r.total_size > MAX_RESOURCE_SIZE * 2:
                raise ValueError("transfer size " + str(r.total_size))
        except Exception as e:
            log("Resource adv invalid, dropping: " + str(e), LOG_ERROR)
            return None

        # Reject what we can't assemble. Multi-segment transfers are what
        # upstream RNS uses for data above its single-segment limit — chaining
        # them here would blow past MAX_RESOURCE_SIZE anyway. Cancel so the
        # sender fails fast instead of both sides stalling until timeout.
        reject = None
        if r.total_data_size > MAX_RESOURCE_SIZE:
            reject = "too large (" + str(r.total_data_size) + "B)"
        elif r.total_segments > 1 or r.segment_index > 1:
            reject = "multi-segment (" + str(r.segment_index) + "/" + str(r.total_segments) + ")"
        if reject:
            log("Resource rejected: " + reject, LOG_ERROR)
            cancel_data = link._token.encrypt(r.hash)
            from .packet import Packet, LinkDestination
            cancel_pkt = Packet(
                LinkDestination(link.link_id), cancel_data,
                const.PKT_DATA, context=const.CTX_RESOURCE_RCL, create_receipt=False,
            )
            cancel_pkt.send()
            return None

        # Parse hashmap
        r.hashmap = []
        for i in range(0, len(hashmap_raw), MAPHASH_LEN):
            r.hashmap.append(hashmap_raw[i:i + MAPHASH_LEN])

        if len(r.hashmap) != r.total_parts:
            log("Resource hashmap mismatch: " + str(len(r.hashmap)) + " != " + str(r.total_parts), LOG_ERROR)
            return None

        # Allocate parts
        r.parts = [None] * r.total_parts
        r.received_count = 0
        r.window_count = 0  # parts received in the current round
        r.last_request_at = 0
        r.last_part_at = 0
        r.retries_left = MAX_RETRIES
        r.progress_callback = None  # callback(resource) — reference RNS signature
        r.sdu = link.sdu
        r.encrypted = None
        r.expected_proof = None
        r.status = TRANSFERRING

        # Adaptive window state (reference Resource parity).
        r.window = WINDOW
        r.window_max = WINDOW_MAX_SLOW
        r.window_min = WINDOW_MIN
        r.window_flexibility = WINDOW_FLEXIBILITY
        r.fast_rate_rounds = 0
        r.very_slow_rate_rounds = 0
        r.round_requested = 0
        r.round_rx_bytes = 0
        r.round_first_sampled = False
        r.req_sent_ms = 0
        r.req_sent_bytes = 0
        r.eifr = 0.0   # measured in-flight rate, B/s
        # Slow-init clamp (urns deviation; mirrors Channel's rtt > RTT_SLOW
        # init): parts are served synchronously into a blocking LoRa TX path,
        # so never let the first unmeasured rounds burst past the very-slow
        # window on a link already known to be slow.
        if (getattr(link, "rtt", 0) or 0) > 1.45:
            r.window_max = WINDOW_MAX_VERY_SLOW

        # Register with link
        link.register_incoming_resource(r)

        log("Resource accepted: " + str(r.total_data_size) + "B, " +
            str(r.total_parts) + " parts, hash=" + r.hash.hex()[:8], LOG_VERBOSE)

        # Request first window
        r.request_next()
        return r

    def _link_ok(self):
        """True while this resource's link can still carry packets.

        A closing link nulls its resources' `link` reference and empties its
        resource tables, so a later send — a watchdog re-advertisement, a part
        request retry, a proof — would raise inside the event loop. Reference
        RNS 1.3.9 added the same pre-send guard (ensure_link); here it also
        stops a doomed transfer from burning LoRa airtime it can never use.
        """
        link = self.link
        if link is None or getattr(link, "status", None) != _LINK_ACTIVE:
            if self.status < COMPLETE:
                log("Resource " + self.hash.hex()[:8] + " link no longer active, cancelling",
                    LOG_VERBOSE)
                self.cancel()
            return False
        return True

    def advertise(self):
        """Send resource advertisement to the remote side."""
        if not self._link_ok():
            return
        adv = {
            "t": len(self.encrypted),
            "d": self.total_data_size,
            "n": self.total_parts,
            "h": self.hash,
            "r": self.random_hash,
            "o": self.hash,  # original_hash = hash (single segment)
            "i": 1,          # segment_index
            "l": 1,          # total_segments
            "q": self.request_id,
            "f": self.flags,
            "m": self.hashmap,
        }
        adv_packed = umsgpack.packb(adv)
        self.link.send(adv_packed, const.CTX_RESOURCE_ADV)
        self.status = ADVERTISED
        self.last_adv_at = time.time()
        log("Resource advertised: " + self.hash.hex()[:8], LOG_DEBUG)

    def check_adv_timeout(self):
        """(Sender) Re-advertise while no part request has arrived. The single
        initial advertisement rides right behind the link proof — over LoRa
        it's a 2-frame split sent while the peer's radio may still be turning
        around from TX, so losing it is common. Without a retry the transfer
        is stillborn: the receiver never learns the resource exists, so its
        own retry machinery never engages. Spacing scales with the link RTT
        (reference: rtt x traffic factor + grace), floored for the 0.25 s job
        tick and capped at the old fixed 15 s; gives up (cancel -> FAILED)
        after MAX_ADV_RETRIES."""
        if self.status != ADVERTISED:
            return
        lrtt = getattr(self.link, "rtt", 0) or 1.0
        interval = lrtt * const.TRAFFIC_TIMEOUT_FACTOR + 1.0
        interval = min(max(interval, 2.0), 15.0)
        if time.time() - self.last_adv_at < interval:
            return
        if self.adv_retries >= MAX_ADV_RETRIES:
            log("Resource adv unanswered after " + str(MAX_ADV_RETRIES) +
                " retries: " + self.hash.hex()[:8], LOG_ERROR)
            self.cancel()
            return
        self.adv_retries += 1
        log("Resource adv retry " + str(self.adv_retries) + "/" +
            str(MAX_ADV_RETRIES) + " for " + self.hash.hex()[:8], LOG_NOTICE)
        self.advertise()

    def request_next(self):
        """(Receiver) Request next window of missing parts."""
        if self.status not in (TRANSFERRING,):
            return
        if not self._link_ok():
            return

        # Find missing parts, up to the current window (bounded so the
        # token-encrypted request always fits one packet: mdu-based).
        try:
            link_mdu = self.link.mdu
        except AttributeError:
            from .link import _link_mdu
            link_mdu = _link_mdu(getattr(self.link, "mtu", const.MTU))
        limit = min(self.window, max(1, (link_mdu - 34) // MAPHASH_LEN))
        missing = []
        for i in range(self.total_parts):
            if self.parts[i] is None:
                missing.append(i)
                if len(missing) >= limit:
                    break

        if not missing:
            self.assemble()
            return

        # Build request: exhausted_flag + [last_map_hash] + resource_hash + requested hashes
        # Check if any missing part has no hashmap entry (needs next segment)
        need_hmu = False
        for i in missing:
            if self.hashmap[i] is None:
                need_hmu = True
                break

        if need_hmu:
            last_map_hash = self.hashmap[self.received_count - 1] if self.received_count > 0 else self.hashmap[0]
            req_data = bytes([HASHMAP_IS_EXHAUSTED])
            req_data += last_map_hash
        else:
            req_data = bytes([HASHMAP_IS_NOT_EXHAUSTED])
        req_data += self.hash
        for i in missing:
            req_data += self.hashmap[i]

        self.window_count = 0
        self.round_requested = len(missing)
        self.round_rx_bytes = 0
        self.round_first_sampled = False
        self.last_request_at = time.time()
        self.req_sent_ms = _ticks_ms()
        self.req_sent_bytes = len(req_data) + _WIRE_OVERHEAD_REQ
        self.link.send(req_data, const.CTX_RESOURCE_REQ)
        log("Resource request: " + str(len(missing)) + " parts (window "
            + str(self.window) + "/" + str(self.window_max) + ") for "
            + self.hash.hex()[:8], LOG_DEBUG)

    def get_progress(self):
        """Return transfer progress as a float 0.0 to 1.0."""
        if self.total_parts == 0:
            return 0.0
        if self.is_initiator:
            return self.sent_count / self.total_parts
        else:
            return self.received_count / self.total_parts

    def check_request_timeout(self):
        """(Receiver) Re-request when a round stalls. The wait scales with the
        measured in-flight rate (or the link RTT before one exists) — a fixed
        interval both crawls on TCP and re-requests parts still on the air at
        SF11, where a window-4 round is ~28 s of airtime."""
        if self.status != TRANSFERRING:
            return
        if self.last_request_at == 0:
            return
        outstanding = max(1, self.round_requested - self.window_count)
        if self.eifr > 0:
            base = PART_TIMEOUT_FACTOR_AFTER_RTT * ((outstanding * self.sdu) / self.eifr)
        else:
            lrtt = getattr(self.link, "rtt", 0) or 1.0
            # ~4x the reference pre-rate branch; deliberately conservative,
            # T_PART_MIN floors it on fast links.
            base = PART_TIMEOUT_FACTOR * 3.5 * lrtt
        retries_used = MAX_RETRIES - self.retries_left
        timeout = base + RETRY_GRACE_TIME + retries_used * PER_RETRY_DELAY
        timeout = min(max(timeout, T_PART_MIN), T_PART_MAX)
        # Parts refresh the clock (reference last_activity): a trickling round
        # is progressing, not stalled.
        anchor = max(self.last_request_at, self.last_part_at)
        if time.time() - anchor < timeout:
            return
        if self.retries_left <= 0:
            log("Resource request retries exhausted: " + self.hash.hex()[:8], LOG_ERROR)
            self.cancel()
            return
        # Reference shrink-on-timeout, nesting exact.
        if self.window > self.window_min:
            self.window -= 1
            if self.window_max > self.window_min:
                self.window_max -= 1
                if (self.window_max - self.window) > (self.window_flexibility - 1):
                    self.window_max -= 1
        self.retries_left -= 1
        log("Resource round stalled, retry (" + str(self.retries_left)
            + " left, window " + str(self.window) + ") for "
            + self.hash.hex()[:8], LOG_DEBUG)
        self.request_next()

    def receive_part(self, data):
        """(Receiver) Receive a raw resource part."""
        if self.status != TRANSFERRING:
            return

        # Match part against hashmap
        part_hash = Identity.full_hash(data + self.random_hash)[:MAPHASH_LEN]

        for i in range(self.total_parts):
            if self.parts[i] is None and self.hashmap[i] == part_hash:
                self.parts[i] = data
                self.received_count += 1
                self.window_count += 1
                self.retries_left = MAX_RETRIES   # progress refunds the budget
                self.last_part_at = time.time()
                self.round_rx_bytes += len(data) + _WIRE_OVERHEAD_PART
                if not self.round_first_sampled and self.req_sent_ms:
                    # First response of the round: request->first-part rate
                    # (reference req_resp sample; feeds the fast counter only).
                    el = _ticks_diff(_ticks_ms(), self.req_sent_ms) / 1000
                    if el > 0:
                        self._rate_sample(
                            (self.req_sent_bytes + len(data) + _WIRE_OVERHEAD_PART) / el,
                            full_round=False)
                    self.round_first_sampled = True
                pct = int(self.received_count * 100 / self.total_parts)
                log("Resource RX " + str(self.received_count) + "/" + str(self.total_parts) +
                    " (" + str(pct) + "%) " + self.hash.hex()[:8], LOG_DEBUG)

                if self.progress_callback:
                    # callback(resource) — reference RNS signature; use
                    # get_progress()/received_count/total_parts on it.
                    try:
                        self.progress_callback(self)
                    except Exception:
                        pass

                if self.received_count == self.total_parts:
                    self.assemble()
                elif self.round_requested and self.window_count >= self.round_requested:
                    self._round_complete()
                return

        log("Resource part hash mismatch, dropping", LOG_DEBUG)

    def _round_complete(self):
        """(Receiver) A full requested round arrived: grow the window, sample
        the round's transfer rate, and request the next round."""
        if self.window < self.window_max:
            self.window += 1
            if (self.window - self.window_min) > (self.window_flexibility - 1):
                self.window_min += 1
        if self.req_sent_ms:
            el = _ticks_diff(_ticks_ms(), self.req_sent_ms) / 1000
            if el > 0:
                rate = self.round_rx_bytes / el
                self._rate_sample(rate, full_round=True)
                self.eifr = rate
        self.window_count = 0
        self.request_next()

    def _rate_sample(self, rate, full_round):
        """Reference fast/very-slow window-cap bookkeeping. The very-slow test
        runs only on full-round samples: first-part samples include the request
        uplink and read systematically low."""
        if rate > RATE_FAST and self.fast_rate_rounds < FAST_RATE_THRESHOLD:
            self.fast_rate_rounds += 1
            if self.fast_rate_rounds == FAST_RATE_THRESHOLD:
                self.window_max = WINDOW_MAX_FAST
                log("Resource " + self.hash.hex()[:8] + " fast link, window max "
                    + str(self.window_max), LOG_DEBUG)
        elif (full_round and self.fast_rate_rounds == 0
                and rate < RATE_VERY_SLOW
                and self.very_slow_rate_rounds < VERY_SLOW_RATE_THRESHOLD):
            self.very_slow_rate_rounds += 1
            if self.very_slow_rate_rounds == VERY_SLOW_RATE_THRESHOLD:
                self.window_max = WINDOW_MAX_VERY_SLOW

    def assemble(self):
        """(Receiver) Assemble all parts, decrypt, verify, and prove."""
        import gc

        self.status = ASSEMBLING
        t0 = time.time()
        log("Resource assembling " + self.hash.hex()[:8], LOG_DEBUG)

        # Join parts
        gc.collect()
        stream = b""
        for p in self.parts:
            stream += p
        self.parts = None  # Free parts list
        gc.collect()

        # Decrypt
        try:
            plaintext = self.link._token.decrypt(stream)
        except Exception as e:
            log("Resource decrypt failed: " + str(e), LOG_ERROR)
            self.status = FAILED
            self._conclude()
            return
        del stream
        gc.collect()
        t1 = time.time()

        # Strip random hash
        received_random = plaintext[:RANDOM_HASH_SIZE]
        self.data = plaintext[RANDOM_HASH_SIZE:]
        del plaintext
        gc.collect()

        # Decompress before verification (hash is of original uncompressed data)
        if self.flags & FLAG_COMPRESSED:
            log("Resource decompressing " + self.hash.hex()[:8] + " (" + str(len(self.data)) + "B compressed)", LOG_DEBUG)
            from .bz2dec import decompress as bz2_decompress
            self.data = bz2_decompress(self.data)
            gc.collect()
        t2 = time.time()

        # Verify hash
        calculated_hash = Identity.full_hash(self.data + self.random_hash)
        if calculated_hash != self.hash:
            log("Resource hash mismatch: " + self.hash.hex()[:8], LOG_ERROR)
            self.status = CORRUPT
            self._conclude()
            return

        # Prove (uses decompressed data)
        self.prove()
        t3 = time.time()
        log("Resource timing: decrypt=" + str(int((t1-t0)*1000)) + "ms decompress=" + str(int((t2-t1)*1000)) + "ms prove=" + str(int((t3-t2)*1000)) + "ms total=" + str(int((t3-t0)*1000)) + "ms", LOG_NOTICE)

        self.status = COMPLETE
        log("Resource complete: " + str(len(self.data)) + "B, hash=" + self.hash.hex()[:8], LOG_NOTICE)
        self._conclude()

    def prove(self):
        """(Receiver) Send proof to sender."""
        proof = Identity.full_hash(self.data + self.hash)
        proof_data = self.hash + proof

        from .packet import Packet, LinkDestination
        proof_pkt = Packet(
            LinkDestination(self.link.link_id), proof_data,
            const.PKT_PROOF, context=const.CTX_RESOURCE_PRF, create_receipt=False,
        )
        proof_pkt.send()
        log("Resource proof sent for " + self.hash.hex()[:8] + " link=" + self.link.link_id.hex()[:8] + " " + str(len(proof_data)) + "B", LOG_NOTICE)

    def validate_proof(self, proof_data):
        """(Sender) Validate proof from receiver."""
        hash_len = 32  # Identity.HASHLENGTH // 8
        if len(proof_data) != hash_len * 2:
            log("Resource proof wrong size: " + str(len(proof_data)), LOG_DEBUG)
            return False

        received_hash = proof_data[:hash_len]
        received_proof = proof_data[hash_len:]

        if received_hash != self.hash:
            log("Resource proof hash mismatch", LOG_DEBUG)
            return False

        if received_proof != self.expected_proof:
            log("Resource proof invalid", LOG_DEBUG)
            return False

        self.status = COMPLETE
        log("Resource transfer complete: " + self.hash.hex()[:8], LOG_NOTICE)

        # Free encrypted data
        self.encrypted = None
        self.parts = None
        import gc; gc.collect()

        self._conclude()
        return True

    def handle_request(self, plaintext):
        """(Sender) Handle part request from receiver."""
        if self.status not in (ADVERTISED, TRANSFERRING):
            return

        # Parse request: exhausted(1) + [last_map(4)] + hash(32) + requested(4 each)
        # NOTE: Link dispatches every resource_req to all outgoing_resources,
        # so we MUST check the hash before mutating state — otherwise we'd
        # transition status / log misleading errors for unrelated resources.
        offset = 0
        exhausted = plaintext[offset]
        offset += 1

        if exhausted == HASHMAP_IS_EXHAUSTED:
            offset += MAPHASH_LEN  # skip last_map_hash

        hash_len = 32
        req_hash = plaintext[offset:offset + hash_len]
        offset += hash_len

        if req_hash != self.hash:
            # Not for us — Link iterates all outgoing resources, so a request
            # for a sibling resource lands here too. Silent return.
            return

        if self.status == ADVERTISED and self.last_adv_at and self.rtt is None:
            # First request answers the advertisement (reference measures
            # rtt = now - adv_sent there).
            rtt = time.time() - self.last_adv_at
            if rtt > 0:
                self.rtt = rtt

        self.status = TRANSFERRING

        # Extract requested part hashes
        requested_hashes = []
        while offset + MAPHASH_LEN <= len(plaintext):
            requested_hashes.append(plaintext[offset:offset + MAPHASH_LEN])
            offset += MAPHASH_LEN

        # Send matching parts. Count unique parts served (not raw TX events)
        # so progress stays in [0, total_parts].
        from .packet import Packet, LinkDestination
        for req_hash_part in requested_hashes:
            for i in range(self.total_parts):
                part_map_hash = self.hashmap[i * MAPHASH_LEN:(i + 1) * MAPHASH_LEN]
                if part_map_hash == req_hash_part:
                    pkt = Packet(
                        LinkDestination(self.link.link_id),
                        self.parts[i],
                        const.PKT_DATA,
                        context=const.CTX_RESOURCE,
                        create_receipt=False,
                    )
                    pkt.MTU = self.link.mtu
                    pkt.send()
                    self.sent_count += 1
                    if not self._parts_served[i]:
                        self._parts_served[i] = True
                        self._unique_parts_served += 1
                    break

        served = self._unique_parts_served
        pct = int(served * 100 / self.total_parts)
        retx = self.sent_count - served
        suffix = " retx=" + str(retx) if retx else ""
        log("Resource TX " + str(served) + "/" + str(self.total_parts) +
            " (" + str(pct) + "%) " + self.hash.hex()[:8] + suffix, LOG_DEBUG)

    def cancel(self, signal=True):
        """Cancel this resource transfer.

        `signal` tells the peer to stop as well: RESOURCE_ICL when we are the
        sender, RESOURCE_RCL when we are the receiver (reference RNS 1.3.9 added
        the receiver-side RCL). Without it the far end keeps re-advertising or
        re-requesting until its own timeout expires — minutes of wasted airtime
        on a half-duplex LoRa channel for a transfer that is already dead. Pass
        False when cancelling *because* the peer just signalled us, so the
        signal does not bounce back.
        """
        if self.status < COMPLETE:
            self.status = FAILED
            if signal:
                self._signal_cancel()
            log("Resource cancelled: " + self.hash.hex()[:8], LOG_DEBUG)
            self._conclude()

    def _signal_cancel(self):
        """Best-effort 'stop sending' to the peer. Never raises: cancellation
        must complete locally even when the link is already gone."""
        link = self.link
        if link is None or getattr(link, "status", None) != _LINK_ACTIVE:
            return
        try:
            from .packet import Packet, LinkDestination
            context = const.CTX_RESOURCE_ICL if self.is_initiator else const.CTX_RESOURCE_RCL
            Packet(
                LinkDestination(link.link_id), link._token.encrypt(self.hash),
                const.PKT_DATA, context=context, create_receipt=False,
            ).send()
        except Exception as e:
            log("Resource cancel signal failed: " + str(e), LOG_DEBUG)

    def is_timed_out(self):
        return time.time() - self.created_at > TIMEOUT

    def _conclude(self):
        """Notify link that this resource is done."""
        try:
            self.link.resource_concluded(self)
        except Exception as e:
            log("Resource conclude error: " + str(e), LOG_ERROR)
