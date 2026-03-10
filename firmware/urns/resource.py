# µReticulum Resource Transfer
# Wire-compatible with reference RNS Resource protocol
# Supports segmented data transfer over Links for payloads > single packet

import time
from . import const, umsgpack
from .log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE
from .identity import Identity
from .crypto.hashes import sha256


# Constants (wire-compatible with reference RNS)
MAPHASH_LEN = 4
RANDOM_HASH_SIZE = 4
WINDOW = 4
MAX_RETRIES = 16
MAX_ADV_RETRIES = 4
TIMEOUT = 120
HASHMAP_IS_EXHAUSTED = 0xFF
HASHMAP_IS_NOT_EXHAUSTED = 0x00
MAX_RESOURCE_SIZE = 16384  # 16KB — ESP32 memory safe

# Resource flags
FLAG_ENCRYPTED = 0x01
FLAG_COMPRESSED = 0x02
FLAG_IS_RESPONSE = 0x10

# States
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

        # Generate random hash
        self.random_hash = Identity.get_random_hash()[:RANDOM_HASH_SIZE]

        # Compute resource hash and expected proof from plaintext
        self.hash = Identity.full_hash(data + self.random_hash)
        self.expected_proof = Identity.full_hash(data + self.hash)

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
        if is_response:
            self.flags |= FLAG_IS_RESPONSE

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

        try:
            adv = umsgpack.unpackb(adv_data)
        except Exception as e:
            log("Resource adv unpack failed: " + str(e), LOG_ERROR)
            return None

        r.total_size = adv["t"]     # encrypted size
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

        # Check size limit
        if r.total_data_size > MAX_RESOURCE_SIZE:
            log("Resource rejected: too large (" + str(r.total_data_size) + "B)", LOG_ERROR)
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
        r.window_count = 0  # parts received since last request
        r.sdu = link.sdu
        r.encrypted = None
        r.expected_proof = None
        r.status = TRANSFERRING

        # Register with link
        link.register_incoming_resource(r)

        log("Resource accepted: " + str(r.total_data_size) + "B, " +
            str(r.total_parts) + " parts, hash=" + r.hash.hex()[:8], LOG_VERBOSE)

        # Request first window
        r.request_next()
        return r

    def advertise(self):
        """Send resource advertisement to the remote side."""
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
        log("Resource advertised: " + self.hash.hex()[:8], LOG_DEBUG)

    def request_next(self):
        """(Receiver) Request next window of missing parts."""
        if self.status not in (TRANSFERRING,):
            return

        # Find missing parts starting from consecutive
        missing = []
        for i in range(self.total_parts):
            if self.parts[i] is None:
                missing.append(i)
                if len(missing) >= WINDOW:
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
        self.link.send(req_data, const.CTX_RESOURCE_REQ)
        log("Resource request: " + str(len(missing)) + " parts for " + self.hash.hex()[:8], LOG_DEBUG)

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
                log("Resource part " + str(i + 1) + "/" + str(self.total_parts) +
                    " for " + self.hash.hex()[:8], LOG_DEBUG)

                if self.received_count == self.total_parts:
                    self.assemble()
                elif self.window_count >= WINDOW:
                    self.window_count = 0
                    self.request_next()
                return

        log("Resource part hash mismatch, dropping", LOG_DEBUG)

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

        self.status = TRANSFERRING

        # Parse request: exhausted(1) + [last_map(4)] + hash(32) + requested(4 each)
        offset = 0
        exhausted = plaintext[offset]
        offset += 1

        if exhausted == HASHMAP_IS_EXHAUSTED:
            offset += MAPHASH_LEN  # skip last_map_hash

        hash_len = 32
        req_hash = plaintext[offset:offset + hash_len]
        offset += hash_len

        if req_hash != self.hash:
            log("Resource request hash mismatch", LOG_DEBUG)
            return

        # Extract requested part hashes
        requested_hashes = []
        while offset + MAPHASH_LEN <= len(plaintext):
            requested_hashes.append(plaintext[offset:offset + MAPHASH_LEN])
            offset += MAPHASH_LEN

        log("Resource sending " + str(len(requested_hashes)) + " parts for " +
            self.hash.hex()[:8], LOG_DEBUG)

        # Send matching parts
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
                    break

    def cancel(self):
        """Cancel this resource transfer."""
        if self.status < COMPLETE:
            self.status = FAILED
            log("Resource cancelled: " + self.hash.hex()[:8], LOG_DEBUG)
            self._conclude()

    def is_timed_out(self):
        return time.time() - self.created_at > TIMEOUT

    def _conclude(self):
        """Notify link that this resource is done."""
        try:
            self.link.resource_concluded(self)
        except Exception as e:
            log("Resource conclude error: " + str(e), LOG_ERROR)
