# µReticulum Link
# Stateful encrypted link (Reticulum Link protocol)
# Server-side (Link) and client-side (OutgoingLink) ECDH handshake + RPC

import struct
import time
from . import const
from .log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE

# Link key sizes
ECPUBSIZE = 64       # X25519(32) + Ed25519(32)
LINK_MTU_SIZE = 3    # Signalling bytes
MTU_BYTEMASK = 0x1FFFFF
MODE_BYTEMASK = 0xE0
MODE_AES256_CBC = 0x01


def _signalling_bytes(mtu, mode):
    sv = (mtu & MTU_BYTEMASK) + (((mode << 5) & MODE_BYTEMASK) << 16)
    return struct.pack(">I", sv)[1:]


def _parse_signalling(data):
    sv = struct.unpack(">I", b'\x00' + data)[0]
    mtu = sv & MTU_BYTEMASK
    mode = (sv >> 21) & 0x07
    return mtu, mode


def _link_mdu(mtu):
    """Max plaintext a single link packet can carry after Token encryption
    (reference RNS Link.update_mdu): floor((mtu-IFAC-HEADER_MIN-TOKEN)/16)*16-1.
    = 431 at MTU 500. Channel MDU is this minus its 6-byte envelope header."""
    import math
    return (math.floor((mtu - const.IFAC_MIN_SIZE - const.HEADER_MINSIZE
                        - const.TOKEN_OVERHEAD) / const.AES128_BLOCKSIZE)
            * const.AES128_BLOCKSIZE) - 1


class Link:
    PENDING = 0x00
    ACTIVE  = 0x01
    CLOSED  = 0x02

    KEEPALIVE_INTERVAL  = 360   # seconds
    STALE_GRACE         = 720   # seconds
    # Establishment timeout scales per hop, like reference RNS
    # (Link.ESTABLISHMENT_TIMEOUT_PER_HOP): each LoRa hop adds airtime and
    # relay latency on top of a base that covers ECDH (~5s per side on ESP32)
    # and proof airtime. 1 hop -> 55s, 2 hops -> 75s, 3 hops -> 95s.
    ESTABLISHMENT_BASE    = 35  # seconds
    ESTABLISHMENT_PER_HOP = 20  # seconds per hop
    CREATION_COOLDOWN   = 5     # min seconds between link creations (allow retries over multi-hop)
    _last_creation      = 0

    def __init__(self, destination, packet):
        from .identity import Identity
        from .crypto import X25519PrivateKey, X25519PublicKey, Token, hkdf

        if len(packet.data) < ECPUBSIZE:
            raise ValueError("Link request too short: " + str(len(packet.data)))

        # Parse peer keys from link request payload
        peer_pub_bytes = packet.data[:32]
        peer_sig_pub_bytes = packet.data[32:64]

        # Parse signalling bytes if present (RNS 0.8+)
        has_signalling = len(packet.data) > ECPUBSIZE
        if has_signalling:
            raw_sig_bytes = packet.data[ECPUBSIZE:ECPUBSIZE + LINK_MTU_SIZE]
            peer_mtu, link_mode = _parse_signalling(raw_sig_bytes)
            # Negotiate: min of peer's proposed MTU and our interface capability
            our_mtu = getattr(packet.receiving_interface, 'HW_MTU', const.MTU) if hasattr(packet, 'receiving_interface') else const.MTU
            self.mtu = min(peer_mtu, our_mtu) if peer_mtu > 0 else our_mtu
            self._signalling_bytes = _signalling_bytes(self.mtu, link_mode)
        else:
            self.mtu = const.MTU
            self._signalling_bytes = b""

        # Compute link_id: strip signalling bytes from hashable part
        # (reference RNS: Link.link_id_from_lr_packet)
        hashable_part = packet.get_hashable_part()
        if has_signalling:
            diff = len(packet.data) - ECPUBSIZE
            hashable_part = hashable_part[:-diff]
        self.link_id = Identity.full_hash(hashable_part)[:const.TRUNCATED_HASHLENGTH // 8]

        self.hash = self.link_id
        self.type = const.DEST_LINK
        self.destination = destination
        self.attached_interface = getattr(packet, 'receiving_interface', None)
        self.status = Link.PENDING
        self.activated_at = None
        self.last_activity = time.time()
        self.last_proof_time = time.time()
        self.establishment_timeout = (Link.ESTABLISHMENT_BASE
                                      + Link.ESTABLISHMENT_PER_HOP * max(1, getattr(packet, "hops", 1)))
        self._callbacks_fired = False
        self.incoming_resources = []
        self.outgoing_resources = []
        self.resource_concluded_callback = None
        self.resource_started_callback = None
        self.remote_identified_callback = None
        self.packet_callback = None
        self.remote_identity = None
        self.sdu = self.mtu - const.HEADER_MAXSIZE - const.IFAC_MIN_SIZE

        # %-formatted in one expression: str + bytes-ish concat chains here
        # raise TypeError when this module is frozen into firmware bytecode.
        _dbg = "Link request on %s link_id=%s mtu=%d hashable=%dB pkt_data=%dB signalling=%s raw[0]=0x%02x" % (
            destination.hexhash[:8], self.link_id.hex()[:8], self.mtu,
            len(hashable_part), len(packet.data),
            self._signalling_bytes.hex() if self._signalling_bytes else "",
            packet.raw[0])
        log(_dbg, LOG_VERBOSE)

        # --- Check capacity and rate limit BEFORE expensive crypto ---
        # ECDH + signing takes ~5s on ESP32, blocking the entire event loop.
        # Reject early to avoid starving poll loops, announces, and replies.
        from .transport import Transport
        if len(Transport.active_links) >= const.MAX_ACTIVE_LINKS:
            evicted = False
            for i, l in enumerate(Transport.active_links):
                if l.status == Link.CLOSED:
                    Transport.active_links.pop(i)
                    evicted = True
                    break
            if not evicted:
                log("Active links table full, rejecting link", LOG_ERROR)
                self.status = Link.CLOSED
                return

        now = time.time()
        if now - Link._last_creation < Link.CREATION_COOLDOWN:
            log("Link request rate limited (" + str(int(Link.CREATION_COOLDOWN - (now - Link._last_creation))) + "s remaining)", LOG_DEBUG)
            self.status = Link.CLOSED
            return

        Link._last_creation = now

        # Generate ephemeral X25519 keypair for ECDH
        import gc; gc.collect()
        import time as _t; _t0 = _t.ticks_ms()
        ephemeral_prv = X25519PrivateKey.generate()
        gc.collect()
        self._ephemeral_pub_bytes = ephemeral_prv.public_key().public_bytes()

        # Compute shared secret via ECDH
        peer_pub = X25519PublicKey.from_public_bytes(peer_pub_bytes)
        shared_key = ephemeral_prv.exchange(peer_pub)
        gc.collect()

        # Derive link encryption key (64 bytes for AES-256 Token)
        derived_key = hkdf(length=64, derive_from=shared_key, salt=self.link_id)
        self._token = Token(derived_key)
        gc.collect()

        log("ECDH completed in " + str(_t.ticks_diff(_t.ticks_ms(), _t0)) + "ms", LOG_DEBUG)

        # Clean up ECDH key material
        del ephemeral_prv, shared_key, derived_key, peer_pub
        gc.collect()

        # Register with Transport
        Transport.active_links.append(self)

        # Send link proof (packet 2 of handshake)
        self._send_proof()

        log("Link " + self.link_id.hex()[:8] + " pending (proof sent)", LOG_VERBOSE)

    def _send_proof(self):
        """Send link proof: signature(64) + ephemeral_pub(32) [+ signalling(3)]"""
        import gc; gc.collect()

        # Reference RNS prove(): signed_data = link_id + pub_bytes + sig_pub_bytes + signalling
        # Where pub_bytes = server's ephemeral X25519 pub
        # And sig_pub_bytes = destination's identity Ed25519 pub
        # (client validates with destination.identity.get_public_key()[32:64])
        signed_data = (self.link_id
                       + self._ephemeral_pub_bytes
                       + self.destination.identity.sig_pub_bytes
                       + self._signalling_bytes)
        signature = self.destination.identity.sign(signed_data)
        gc.collect()

        proof_data = signature + self._ephemeral_pub_bytes + self._signalling_bytes

        from .packet import Packet
        proof_packet = Packet(
            self, proof_data,
            const.PKT_PROOF,
            context=const.CTX_LRPROOF,
            context_flag=const.FLAG_UNSET,
            create_receipt=False,
            attached_interface=self.attached_interface,
        )
        proof_packet.send()

        # Clean up (no longer needed after proof)
        del self._ephemeral_pub_bytes, self._signalling_bytes
        gc.collect()

    def receive(self, packet):
        """Handle incoming data packet on this link."""
        # Raw resource parts — NOT Token-encrypted
        if packet.context == const.CTX_RESOURCE:
            self.last_activity = time.time()
            for r in self.incoming_resources:
                r.receive_part(packet.data)
            return

        # Keepalives are raw 1-byte probes (0xFF -> reply 0xFE), never
        # Token-encrypted — handle before decrypt (reference RNS Link).
        if packet.context == const.CTX_KEEPALIVE:
            self.last_activity = time.time()
            if packet.data == b"\xff":
                from .packet import Packet, LinkDestination
                Packet(
                    LinkDestination(self.link_id), b"\xfe",
                    const.PKT_DATA, context=const.CTX_KEEPALIVE,
                    create_receipt=False,
                ).send()
                log("Link " + self.link_id.hex()[:8] + " keepalive answered", LOG_DEBUG)
            return

        try:
            plaintext = self._token.decrypt(packet.data)
        except Exception as e:
            log("Link " + self.link_id.hex()[:8] + " decrypt failed ctx=0x"
                + ("%02x" % packet.context) + " data=" + str(len(packet.data))
                + "B: " + str(e), LOG_DEBUG)
            return

        self.last_activity = time.time()
        log("Link " + self.link_id.hex()[:8] + " ctx=0x" + ("%02x" % packet.context) + " " + str(len(plaintext)) + "B", LOG_DEBUG)

        if packet.context == const.CTX_LRRTT:
            self._handle_rtt(plaintext)
        elif packet.context == const.CTX_REQUEST:
            self._handle_request(plaintext, packet)
        elif packet.context == const.CTX_RESOURCE_ADV:
            self._handle_resource_adv(plaintext)
        elif packet.context == const.CTX_RESOURCE_REQ:
            self._handle_resource_req(plaintext)
        elif packet.context == const.CTX_RESOURCE_HMU:
            log("Link " + self.link_id.hex()[:8] + " hashmap update (not supported)", LOG_DEBUG)
        elif packet.context == const.CTX_RESOURCE_ICL:
            self._handle_resource_cancel(plaintext)
        elif packet.context == const.CTX_RESOURCE_RCL:
            self._handle_resource_cancel(plaintext)
        elif packet.context == const.CTX_LINKCLOSE:
            log("Link " + self.link_id.hex()[:8] + " close received", LOG_VERBOSE)
            self.status = Link.CLOSED
        elif packet.context == const.CTX_LINKIDENTIFY:
            self._handle_identify(plaintext)
        elif packet.context == const.CTX_NONE:
            if self.packet_callback:
                try:
                    self.packet_callback(plaintext, packet)
                except Exception as e:
                    log("Link " + self.link_id.hex()[:8] + " packet callback error: " + str(e), LOG_ERROR)
            else:
                log("Link " + self.link_id.hex()[:8] + " data packet, no callback", LOG_DEBUG)
            self.prove_packet(packet)
        else:
            log("Link " + self.link_id.hex()[:8] + " unhandled context=0x" + ("%02x" % packet.context), LOG_DEBUG)

    def _handle_rtt(self, plaintext):
        """RTT packet marks link as ACTIVE (packet 3 of handshake)."""
        if self.status == Link.PENDING:
            self.status = Link.ACTIVE
            self.activated_at = time.time()
            log("Link " + self.link_id.hex()[:8] + " ACTIVE", LOG_NOTICE)

            if not self._callbacks_fired and self.destination.link_established_callback:
                self._callbacks_fired = True
                try:
                    self.destination.link_established_callback(self)
                except Exception as e:
                    log("Link established callback error: " + str(e), LOG_ERROR)

    def _handle_identify(self, plaintext):
        """Handle incoming link identification from the initiator."""
        from .identity import Identity
        keysize = Identity.KEYSIZE // 8    # 64 bytes (enc_pub + sig_pub)
        sigsize = Identity.SIGLENGTH // 8  # 64 bytes

        if len(plaintext) != keysize + sigsize:
            log("Link " + self.link_id.hex()[:8] + " identify: wrong length " + str(len(plaintext)), LOG_DEBUG)
            return

        public_key = plaintext[:keysize]
        signature = plaintext[keysize:keysize + sigsize]
        signed_data = self.link_id + public_key

        identity = Identity(create_keys=False)
        identity.load_public_key(public_key)

        if identity.validate(signature, signed_data):
            self.remote_identity = identity
            log("Link " + self.link_id.hex()[:8] + " identified as " + identity.hexhash[:8], LOG_VERBOSE)
            if self.remote_identified_callback:
                try:
                    self.remote_identified_callback(self, identity)
                except Exception as e:
                    log("Link " + self.link_id.hex()[:8] + " identify callback error: " + str(e), LOG_ERROR)
        else:
            log("Link " + self.link_id.hex()[:8] + " identify: invalid signature", LOG_DEBUG)

    def set_remote_identified_callback(self, callback):
        self.remote_identified_callback = callback

    def get_remote_identity(self):
        return self.remote_identity

    def _handle_request(self, plaintext, packet):
        """Handle incoming request on established link."""
        if self.status != Link.ACTIVE:
            log("Link " + self.link_id.hex()[:8] + " request on non-active link, ignoring", LOG_DEBUG)
            return

        from . import umsgpack

        try:
            request_data = umsgpack.unpackb(plaintext)
        except Exception as e:
            log("Link " + self.link_id.hex()[:8] + " request unpack error: " + str(e), LOG_DEBUG)
            return

        if not isinstance(request_data, list) or len(request_data) < 2:
            log("Link " + self.link_id.hex()[:8] + " malformed request", LOG_DEBUG)
            return

        # Request format: [timestamp, path_hash, data]
        requested_at = request_data[0]
        path_hash = request_data[1]
        req_data = request_data[2] if len(request_data) > 2 else None

        # Compute request_id from the packet's truncated hash
        request_id = packet.getTruncatedHash()

        log("Link " + self.link_id.hex()[:8] + " request path_hash=" + path_hash.hex()[:8], LOG_DEBUG)

        # Look up handler by path_hash
        handler_entry = self.destination.request_handlers.get(path_hash)
        if handler_entry is None:
            log("Link " + self.link_id.hex()[:8] + " no handler for path", LOG_DEBUG)
            return

        from .destination import Destination
        if handler_entry["allow"] == Destination.ALLOW_NONE:
            log("Link " + self.link_id.hex()[:8] + " request denied by policy", LOG_DEBUG)
            return

        # Call the response generator
        try:
            response = handler_entry["generator"](
                path=handler_entry.get("path", ""),
                data=req_data,
                request_id=request_id,
                link_id=self.link_id,
                remote_identity=None,
                requested_at=requested_at,
            )
        except Exception as e:
            log("Link " + self.link_id.hex()[:8] + " handler error: " + str(e), LOG_ERROR)
            return

        if response is None:
            return

        # Pack response: [request_id, response_data]
        response_packed = umsgpack.packb([request_id, response])

        # Max plaintext for a single link data packet:
        # MTU(500) - HDR_1(19) - IV(16) - max_PKCS7(16) - HMAC(32) = 417
        if len(response_packed) > 417:
            import gc
            gc.collect()
            from .resource import Resource
            log("Link " + self.link_id.hex()[:8] + " response " + str(len(response_packed)) + "B, using Resource", LOG_VERBOSE)
            Resource(self, response_packed, is_response=True, request_id=request_id)
            return

        self.send(response_packed, const.CTX_RESPONSE)
        log("Link " + self.link_id.hex()[:8] + " response sent (" + str(len(response_packed)) + "B)", LOG_DEBUG)

    def _handle_resource_adv(self, plaintext):
        """Handle incoming resource advertisement (receiver mode)."""
        from .resource import Resource, MAX_RESOURCE_SIZE
        if len(self.incoming_resources) >= const.MAX_INCOMING_RESOURCES:
            log("Link " + self.link_id.hex()[:8] + " too many incoming resources", LOG_DEBUG)
            return
        r = Resource.accept(plaintext, self)
        if r and self.resource_started_callback:
            try:
                self.resource_started_callback(r)
            except Exception as e:
                log("Resource started callback error: " + str(e), LOG_ERROR)

    def _handle_resource_req(self, plaintext):
        """Handle resource part request (sender mode)."""
        for r in self.outgoing_resources:
            r.handle_request(plaintext)

    def _handle_resource_prf(self, proof_data):
        """Handle resource proof (sender mode). Called from transport.
        Routed by resource hash so stale transfers don't log mismatches."""
        rhash = proof_data[:32]
        for r in list(self.outgoing_resources):
            if r.hash == rhash:
                r.validate_proof(proof_data)
                return

    def _handle_resource_cancel(self, plaintext):
        """Handle resource cancel from remote side."""
        # plaintext = resource hash
        for r in list(self.incoming_resources):
            if r.hash == plaintext:
                r.cancel()
                return
        for r in list(self.outgoing_resources):
            if r.hash == plaintext:
                r.cancel()
                return

    def register_incoming_resource(self, resource):
        self.incoming_resources.append(resource)

    def register_outgoing_resource(self, resource):
        self.outgoing_resources.append(resource)

    def resource_concluded(self, resource):
        """Called when a resource transfer completes or fails."""
        if resource in self.incoming_resources:
            self.incoming_resources.remove(resource)
        if resource in self.outgoing_resources:
            self.outgoing_resources.remove(resource)
        if self.resource_concluded_callback:
            try:
                self.resource_concluded_callback(resource)
            except Exception as e:
                log("Resource concluded callback error: " + str(e), LOG_ERROR)

    def prove_packet(self, packet):
        """Send explicit proof for a packet received on this link."""
        signature = self.destination.identity.sign(packet.packet_hash)
        proof_data = packet.packet_hash + signature
        from .packet import Packet, LinkDestination
        proof = Packet(
            LinkDestination(self.link_id), proof_data,
            const.PKT_PROOF, create_receipt=False,
        )
        proof.send()
        log("Link " + self.link_id.hex()[:8] + " proof sent for " + packet.packet_hash.hex()[:8], LOG_DEBUG)

    def set_packet_callback(self, callback):
        self.packet_callback = callback

    def set_resource_started_callback(self, callback):
        """callback(resource) — fired when an incoming resource is accepted
        (reference RNS API parity)."""
        self.resource_started_callback = callback

    def set_resource_concluded_callback(self, callback):
        """callback(resource) — fired when a resource transfer concludes
        (reference RNS API parity)."""
        self.resource_concluded_callback = callback

    def send(self, data, context=const.CTX_NONE):
        """Send encrypted data on this link."""
        ciphertext = self._token.encrypt(data)

        from .packet import Packet, LinkDestination
        packet = Packet(
            LinkDestination(self.link_id),
            ciphertext,
            const.PKT_DATA,
            context=context,
            create_receipt=False,
        )
        packet.MTU = self.mtu
        packet.send()

    def check_keepalive(self):
        """Check link staleness and send keepalive if needed."""
        now = time.time()

        # Check establishment timeout for pending links
        if self.status == Link.PENDING:
            if now - self.last_proof_time > self.establishment_timeout:
                log("Link " + self.link_id.hex()[:8] + " establishment timeout", LOG_VERBOSE)
                self.status = Link.CLOSED
            return

        if self.status != Link.ACTIVE:
            return

        # Check resource request timeouts (retry if no parts arrived)
        for r in self.incoming_resources:
            r.check_request_timeout()

        # Sender-side watchdog for outgoing resources: re-advertise while the
        # advertisement goes unanswered, and fail transfers that exceed the
        # overall timeout — otherwise a stall wedges the link forever and
        # later DIRECT sends reuse the dead link and stick too.
        for r in list(self.outgoing_resources):
            r.check_adv_timeout()
            if r.is_timed_out():
                r.cancel()

        # Check stale grace period
        if now - self.last_activity > Link.STALE_GRACE:
            log("Link " + self.link_id.hex()[:8] + " stale, closing", LOG_VERBOSE)
            self.teardown()

    def teardown(self):
        """Close this link."""
        if self.status != Link.CLOSED:
            self.status = Link.CLOSED
            log("Link " + self.link_id.hex()[:8] + " torn down", LOG_VERBOSE)
        # Break circular refs (MicroPython GC can't collect cycles)
        self.destination = None
        self.packet_callback = None
        self.resource_concluded_callback = None
        self.resource_started_callback = None
        self.remote_identified_callback = None
        for r in self.incoming_resources:
            r.link = None
        for r in self.outgoing_resources:
            r.link = None
        self.incoming_resources = []
        self.outgoing_resources = []

    def __repr__(self):
        states = {0: "PENDING", 1: "ACTIVE", 2: "CLOSED"}
        return "<Link:" + self.link_id.hex()[:8] + " " + states.get(self.status, "?") + ">"


# pending_requests entry indices (OutgoingLink.request)
_PR_STATE   = 0
_PR_SENT_AT = 1
_PR_TIMEOUT = 2
_PR_RESP_CB = 3
_PR_FAIL_CB = 4
_PR_PROG_CB = 5


class OutgoingLink:
    """Client-side link — initiates ECDH handshake to a remote destination."""

    PENDING = 0x00
    ACTIVE  = 0x01
    CLOSED  = 0x02
    # Per-hop establishment timeout, same rationale as Link above.
    ESTABLISHMENT_BASE    = 35  # seconds
    ESTABLISHMENT_PER_HOP = 20  # seconds per hop

    # Pending request states: SENT waits for a response packet or resource
    # advertisement (request timeout applies); RECEIVING means the response
    # is arriving as a resource — its own retry/cancel machinery governs
    # failure, so the request timeout is suspended.
    REQ_SENT      = 0x00
    REQ_RECEIVING = 0x01
    REQUEST_TIMEOUT_BASE    = 30  # seconds, + per-hop below
    REQUEST_TIMEOUT_PER_HOP = 15

    def __init__(self, destination, established_callback=None, closed_callback=None,
                 sign_proofs=False):
        from .identity import Identity
        from .crypto import X25519PrivateKey
        import gc, os

        self.destination = destination
        self.status = OutgoingLink.PENDING
        self.established_callback = established_callback
        self.closed_callback = closed_callback
        self._token = None
        self.activated_at = None
        self.last_activity = time.time()
        self.request_time = time.time()
        self.rtt = 0                 # measured at handshake (validate_proof)
        self.type = const.DEST_LINK
        self.incoming_resources = []
        self.outgoing_resources = []
        self.pending_requests = {}  # request_id -> [state, sent_at, timeout, resp_cb, fail_cb, prog_cb]
        self.resource_concluded_callback = None
        self.resource_started_callback = None
        self.remote_identified_callback = None
        self.packet_callback = None
        self.remote_identity = None
        # Channel (rnsh etc.) — created lazily via get_channel(). Keepalive.
        self._channel = None
        self._last_keepalive = time.time()
        # LRRTT delivery tracking (see validate_proof / check_keepalive).
        self._lrrtt_data = None
        self._lrrtt_confirmed = False
        self._lrrtt_resends = 0
        self._lrrtt_last = 0

        from .transport import Transport
        self.establishment_timeout = (OutgoingLink.ESTABLISHMENT_BASE
                                      + OutgoingLink.ESTABLISHMENT_PER_HOP
                                      * max(1, Transport.hops_to(destination.hash)))

        # Generate ephemeral X25519 keypair for ECDH
        gc.collect()
        self._prv = X25519PrivateKey.generate()
        gc.collect()
        self._pub_bytes = self._prv.public_key().public_bytes()

        # Ed25519 keypair for the link. Normally the server never verifies the
        # client's Ed25519, so we fill the request's sig-pub slot with random
        # bytes (only used in the link_id hash) and skip ~2s of keygen. But a
        # Channel ACKs data by PROVING packets, which the peer validates against
        # this sig-pub — so when sign_proofs is set (rnsh) we generate a REAL
        # ephemeral keypair and keep the private key for prove_packet().
        if sign_proofs:
            from .crypto import Ed25519PrivateKey
            self._sig_prv = Ed25519PrivateKey.generate()
            gc.collect()
            self._sig_pub_bytes = self._sig_prv.public_key().public_bytes()
        else:
            self._sig_prv = None
            self._sig_pub_bytes = os.urandom(32)

        # Signalling bytes (our MTU, AES-256-CBC)
        self.mtu = const.MTU
        self.sdu = self.mtu - const.HEADER_MAXSIZE - const.IFAC_MIN_SIZE
        self.mdu = _link_mdu(self.mtu)
        sig_bytes = _signalling_bytes(const.MTU, MODE_AES256_CBC)

        # Build and send link request
        request_data = self._pub_bytes + self._sig_pub_bytes + sig_bytes

        from .packet import Packet
        request_packet = Packet(
            destination, request_data,
            const.PKT_LINKREQUEST,
        )
        request_packet.pack()

        # Compute link_id (hash of request excluding signalling bytes)
        hashable_part = request_packet.get_hashable_part()
        diff = len(request_data) - ECPUBSIZE
        hashable_part = hashable_part[:-diff]
        self.link_id = Identity.full_hash(hashable_part)[:const.TRUNCATED_HASHLENGTH // 8]
        self.hash = self.link_id

        # Register as pending
        from .transport import Transport
        Transport.pending_links.append(self)

        request_packet.send()
        log("OutLink request to " + destination.hexhash[:8] + " link_id=" + self.link_id.hex()[:8], LOG_VERBOSE)

    def validate_proof(self, packet):
        """Validate server's link proof, complete ECDH handshake, send RTT."""
        from .crypto import X25519PublicKey, Token, hkdf
        from .identity import Identity
        import gc

        proof_data = packet.data
        sig_len = 64
        key_len = 32

        # Discard malformed/corrupt LRPROOFs without killing the link. A
        # second, intact copy of the same proof may still arrive (multi-path
        # RF, transport relay echo). If none ever does, the establishment
        # timeout will close us.
        if len(proof_data) < sig_len + key_len:
            log("OutLink proof too short: " + str(len(proof_data)) + ", ignoring", LOG_DEBUG)
            return

        # Tentative parse — do NOT mutate self.mtu/sdu until signature verifies,
        # otherwise a corrupt proof can silently shrink our MTU.
        signature = proof_data[:sig_len]
        peer_ecdh_pub_bytes = proof_data[sig_len:sig_len + key_len]
        signalling_bytes = proof_data[sig_len + key_len:] if len(proof_data) > sig_len + key_len else b""

        # Verify server's signature: sign(link_id + server_ecdh_pub + server_ed25519_pub + signalling)
        peer_sig_pub_bytes = self.destination.identity.sig_pub_bytes
        signed_data = self.link_id + peer_ecdh_pub_bytes + peer_sig_pub_bytes + signalling_bytes

        gc.collect()
        if not self.destination.identity.validate(signature, signed_data):
            log("OutLink proof signature invalid, ignoring (link " + self.link_id.hex()[:8] + " still pending)", LOG_DEBUG)
            return
        gc.collect()

        # Signature OK — commit MTU negotiation.
        if signalling_bytes:
            peer_mtu, _ = _parse_signalling(signalling_bytes)
            if peer_mtu > 0:
                self.mtu = min(self.mtu, peer_mtu)
                self.sdu = self.mtu - const.HEADER_MAXSIZE - const.IFAC_MIN_SIZE
                self.mdu = _link_mdu(self.mtu)

        # ECDH key exchange
        peer_pub = X25519PublicKey.from_public_bytes(peer_ecdh_pub_bytes)
        shared_key = self._prv.exchange(peer_pub)
        gc.collect()

        # Derive link encryption key
        derived_key = hkdf(length=64, derive_from=shared_key, salt=self.link_id)
        self._token = Token(derived_key)
        gc.collect()

        # Clean up key material
        del self._prv, shared_key, derived_key, peer_pub
        gc.collect()

        # Send RTT to complete handshake (server marks link ACTIVE on receiving this)
        from . import umsgpack
        rtt = time.time() - self.request_time
        # Coarse establishment-time RTT (includes path/relay latency): a safe
        # over-estimate used to size Channel windows/timeouts and keepalive.
        self.rtt = rtt
        rtt_data = umsgpack.packb(rtt)
        self.send(rtt_data, const.CTX_LRRTT)

        # The LRRTT is the peer's ONLY trigger to mark the link established
        # (reference RNS fires link_established solely on receiving it), yet it
        # is a single fire-and-forget packet on a half-duplex medium. When this
        # link is a reply to a message we just proved, the relay upstream is
        # typically still transmitting the sibling link's traffic on LoRa the
        # moment we answer the proof — its radio is deaf and the LRRTT dies.
        # The peer then answers raw keepalives (status-independent) but
        # silently ignores every resource advertisement (ACCEPT_NONE until
        # link_established), so the link looks alive while nothing delivers.
        # Keep the RTT payload for resend until the peer proves establishment
        # by sending anything that decrypts (it only encrypts to us after
        # processing our LRRTT).
        self._lrrtt_data = rtt_data
        self._lrrtt_confirmed = False
        self._lrrtt_resends = 0
        self._lrrtt_last = time.time()

        # Mark active
        self.status = OutgoingLink.ACTIVE
        self.activated_at = time.time()
        self.last_activity = time.time()
        self._last_keepalive = time.time()

        # Move from pending to active
        from .transport import Transport
        if self in Transport.pending_links:
            Transport.pending_links.remove(self)
        Transport.active_links.append(self)

        log("OutLink " + self.link_id.hex()[:8] + " ACTIVE (rtt=" + str(int(rtt * 1000)) + "ms)", LOG_NOTICE)

        if self.established_callback:
            try:
                self.established_callback(self)
            except Exception as e:
                log("OutLink established callback error: " + str(e), LOG_ERROR)

    def send(self, data, context=const.CTX_NONE):
        """Send encrypted data on this link."""
        ciphertext = self._token.encrypt(data)
        from .packet import Packet, LinkDestination
        packet = Packet(
            LinkDestination(self.link_id), ciphertext,
            const.PKT_DATA, context=context, create_receipt=False,
        )
        packet.send()

    # --- Channel (rnsh etc.) ------------------------------------------------

    def get_channel(self):
        """Return this link's Channel (reliable ordered message stream),
        creating it on first use. rnsh and RNS.Buffer ride on this."""
        if self._channel is None:
            from .channel import Channel, LinkChannelOutlet
            self._channel = Channel(LinkChannelOutlet(self))
        return self._channel

    def _handle_channel(self, plaintext, packet):
        """Deliver a CHANNEL packet to the channel. Prove it first so the peer's
        Channel gets its ACK and advances its window (reference Link.receive)."""
        if self._channel is None:
            log("OutLink " + self.link_id.hex()[:8] + " channel data, no channel", LOG_DEBUG)
            return
        self.prove_packet(packet)
        self._channel._receive(plaintext)

    def prove_packet(self, packet):
        """Explicitly prove a received packet (the Channel ACK): sign its hash
        with our ephemeral link key. The peer validates the signature against
        the sig-pub we put in the link request. No-op unless sign_proofs=True."""
        if self._sig_prv is None:
            log("OutLink " + self.link_id.hex()[:8] + " cannot prove (no sig key)", LOG_DEBUG)
            return
        signature = self._sig_prv.sign(packet.packet_hash)
        proof_data = packet.packet_hash + signature
        from .packet import Packet, LinkDestination
        Packet(LinkDestination(self.link_id), proof_data,
               const.PKT_PROOF, create_receipt=False).send()

    def identify(self, identity):
        """Identify this initiator to the peer (reference Link.identify): send
        our public key + a signature over link_id||pubkey, Token-encrypted.
        rnsh listeners use the identified identity for authorization."""
        if self.status != OutgoingLink.ACTIVE:
            return
        pub = identity.get_public_key()
        signature = identity.sign(self.link_id + pub)
        self.send(pub + signature, const.CTX_LINKIDENTIFY)
        log("OutLink " + self.link_id.hex()[:8] + " identified as " + identity.hexhash[:8], LOG_VERBOSE)

    def _confirm_lrrtt(self):
        """Mark the LRRTT as delivered: the peer sent something Token-encrypted
        (or a resource part), which it only does once its side of the link is
        established — i.e. our LRRTT was processed. Stops the resend loop."""
        if not self._lrrtt_confirmed:
            self._lrrtt_confirmed = True
            self._lrrtt_data = None

    def _keepalive_interval(self):
        """Seconds between idle keepalives. The peer (non-initiator) stales us
        if it hears nothing for ~2x its own rtt-scaled keepalive: ~10s on a fast
        link, ~720s on LoRa. Our establishment-rtt is a coarse over-estimate, so
        we pick per-regime instead of scaling it (scaling would overshoot the
        fast peer's stale window and get us dropped): 5s fast, 300s slow."""
        return 300 if (self.rtt and self.rtt >= 1.45) else 5

    def _send_keepalive(self):
        from .packet import Packet, LinkDestination
        try:
            Packet(LinkDestination(self.link_id), b"\xff",
                   const.PKT_DATA, context=const.CTX_KEEPALIVE,
                   create_receipt=False).send()
            log("OutLink " + self.link_id.hex()[:8] + " keepalive sent", LOG_DEBUG)
        except Exception as e:
            log("OutLink keepalive send error: " + str(e), LOG_DEBUG)

    def request(self, path, data=None, response_callback=None,
                failed_callback=None, progress_callback=None, timeout=None):
        """Send a request over this link (reference RNS Link.request parity).

        Returns the request_id (bytes) or None if the request could not be
        sent. On success response_callback(request_id, response_data) fires;
        on timeout or failure failed_callback(request_id) fires. Responses
        larger than one packet arrive as a Resource and are dispatched
        transparently. The request payload itself must fit in one packet.
        """
        from .identity import Identity
        from . import umsgpack

        if self.status != OutgoingLink.ACTIVE:
            return None

        path_hash = Identity.truncated_hash(path.encode("utf-8"))
        packed = umsgpack.packb([time.time(), path_hash, data])
        if len(packed) > self.sdu:
            log("Link request too large: " + str(len(packed)) + "B > " + str(self.sdu) + "B sdu", LOG_ERROR)
            return None

        if timeout is None:
            from .transport import Transport
            hops = max(1, Transport.hops_to(self.destination.hash))
            timeout = OutgoingLink.REQUEST_TIMEOUT_BASE + OutgoingLink.REQUEST_TIMEOUT_PER_HOP * hops

        ciphertext = self._token.encrypt(packed)
        from .packet import Packet, LinkDestination
        packet = Packet(
            LinkDestination(self.link_id), ciphertext,
            const.PKT_DATA, context=const.CTX_REQUEST, create_receipt=False,
        )
        if packet.send() is False:
            return None

        # Same id the responder derives from the received packet — the
        # hashable part is transport-invariant (see Packet.get_hashable_part).
        request_id = packet.getTruncatedHash()
        self.pending_requests[request_id] = [
            OutgoingLink.REQ_SENT, time.time(), timeout,
            response_callback, failed_callback, progress_callback,
        ]
        log("OutLink " + self.link_id.hex()[:8] + " request " + path
            + " id=" + request_id.hex()[:8], LOG_VERBOSE)
        return request_id

    def _handle_response(self, plaintext):
        """Handle a single-packet response to one of our requests."""
        from . import umsgpack
        try:
            unpacked = umsgpack.unpackb(plaintext)
        except Exception as e:
            log("OutLink response unpack error: " + str(e), LOG_DEBUG)
            return
        if not isinstance(unpacked, list) or len(unpacked) < 2:
            return
        self._dispatch_response(unpacked[0], unpacked[1])

    def _dispatch_response(self, request_id, response_data):
        entry = self.pending_requests.pop(request_id, None)
        if entry is None:
            return
        log("OutLink " + self.link_id.hex()[:8] + " response id=" + request_id.hex()[:8], LOG_VERBOSE)
        if entry[_PR_RESP_CB]:
            try:
                entry[_PR_RESP_CB](request_id, response_data)
            except Exception as e:
                log("Response callback error: " + str(e), LOG_ERROR)

    def _fail_request(self, request_id, reason=""):
        entry = self.pending_requests.pop(request_id, None)
        if entry is None:
            return
        if reason:
            log("OutLink request " + request_id.hex()[:8] + " failed: " + reason, LOG_VERBOSE)
        if entry[_PR_FAIL_CB]:
            try:
                entry[_PR_FAIL_CB](request_id)
            except Exception as e:
                log("Request failed callback error: " + str(e), LOG_ERROR)

    def _fail_rejected_response(self, adv_data):
        """A resource advertisement could not be accepted (too large,
        multi-segment, unparseable). If it carried the request_id of one of
        our pending requests, fail it now — the response can never arrive."""
        from . import umsgpack
        try:
            adv = umsgpack.unpackb(adv_data)
            request_id = adv.get("q") if isinstance(adv, dict) else None
        except Exception:
            return
        if request_id:
            self._fail_request(request_id, "response rejected")

    def receive(self, packet):
        """Handle incoming data on this link."""
        if packet.context == const.CTX_RESOURCE:
            self.last_activity = time.time()
            self._confirm_lrrtt()
            for r in self.incoming_resources:
                r.receive_part(packet.data)
            return

        # Keepalive responses (0xFE) are raw, never Token-encrypted — the
        # peer answers our 0xFF probes; just refresh activity.
        if packet.context == const.CTX_KEEPALIVE:
            self.last_activity = time.time()
            return

        try:
            plaintext = self._token.decrypt(packet.data)
        except Exception as e:
            log("OutLink " + self.link_id.hex()[:8] + " decrypt failed: " + str(e), LOG_DEBUG)
            return

        self.last_activity = time.time()
        self._confirm_lrrtt()

        if packet.context == const.CTX_RESOURCE_ADV:
            self._handle_resource_adv(plaintext)
        elif packet.context == const.CTX_RESPONSE:
            self._handle_response(plaintext)
        elif packet.context == const.CTX_RESOURCE_REQ:
            self._handle_resource_req(plaintext)
        elif packet.context == const.CTX_RESOURCE_ICL or packet.context == const.CTX_RESOURCE_RCL:
            self._handle_resource_cancel(plaintext)
        elif packet.context == const.CTX_CHANNEL:
            self._handle_channel(plaintext, packet)
        elif packet.context == const.CTX_LINKCLOSE:
            log("OutLink " + self.link_id.hex()[:8] + " close received", LOG_VERBOSE)
            self._close()
        elif packet.context == const.CTX_NONE:
            if self.packet_callback:
                try:
                    self.packet_callback(plaintext, packet)
                except Exception as e:
                    log("OutLink packet callback error: " + str(e), LOG_ERROR)
        else:
            log("OutLink " + self.link_id.hex()[:8] + " ctx=0x" + ("%02x" % packet.context), LOG_DEBUG)

    def _handle_resource_adv(self, plaintext):
        from .resource import Resource, MAX_RESOURCE_SIZE
        if len(self.incoming_resources) >= const.MAX_INCOMING_RESOURCES:
            return
        r = Resource.accept(plaintext, self)
        if r is None:
            self._fail_rejected_response(plaintext)
            return
        if r.request_id and r.request_id in self.pending_requests:
            # Response to one of our requests arriving as a resource: from
            # here the resource retry machinery governs failure, so suspend
            # the request timeout and route progress to the requester.
            entry = self.pending_requests[r.request_id]
            entry[_PR_STATE] = OutgoingLink.REQ_RECEIVING
            if entry[_PR_PROG_CB]:
                r.progress_callback = entry[_PR_PROG_CB]
        elif self.resource_started_callback:
            try:
                self.resource_started_callback(r)
            except Exception as e:
                log("Resource started callback error: " + str(e), LOG_ERROR)

    def set_resource_started_callback(self, callback):
        """callback(resource) — reference RNS API parity."""
        self.resource_started_callback = callback

    def set_resource_concluded_callback(self, callback):
        """callback(resource) — reference RNS API parity."""
        self.resource_concluded_callback = callback

    def _handle_resource_req(self, plaintext):
        for r in self.outgoing_resources:
            r.handle_request(plaintext)

    def _handle_resource_prf(self, proof_data):
        rhash = proof_data[:32]
        for r in list(self.outgoing_resources):
            if r.hash == rhash:
                r.validate_proof(proof_data)
                return

    def _handle_resource_cancel(self, plaintext):
        for r in list(self.incoming_resources):
            if r.hash == plaintext:
                r.cancel()
                return
        for r in list(self.outgoing_resources):
            if r.hash == plaintext:
                r.cancel()
                return

    def register_incoming_resource(self, resource):
        self.incoming_resources.append(resource)

    def register_outgoing_resource(self, resource):
        self.outgoing_resources.append(resource)

    def resource_concluded(self, resource):
        if resource in self.incoming_resources:
            self.incoming_resources.remove(resource)
        if resource in self.outgoing_resources:
            self.outgoing_resources.remove(resource)
        # Response to one of our requests — dispatch it instead of the
        # generic resource callback.
        rid = resource.request_id
        if rid and rid in self.pending_requests:
            from .resource import COMPLETE
            if resource.status == COMPLETE:
                from . import umsgpack
                try:
                    unpacked = umsgpack.unpackb(resource.data)
                except Exception:
                    unpacked = None
                if isinstance(unpacked, list) and len(unpacked) >= 2:
                    self._dispatch_response(rid, unpacked[1])
                else:
                    self._fail_request(rid, "malformed response")
            else:
                self._fail_request(rid, "response transfer failed")
            return
        if self.resource_concluded_callback:
            try:
                self.resource_concluded_callback(resource)
            except Exception as e:
                log("Resource concluded callback error: " + str(e), LOG_ERROR)

    def check_keepalive(self):
        """Check link staleness (called by transport job_loop)."""
        if self.status == OutgoingLink.PENDING:
            if time.time() - self.request_time > self.establishment_timeout:
                log("OutLink " + self.link_id.hex()[:8] + " establishment timeout", LOG_VERBOSE)
                self._close()
            return
        if self.status != OutgoingLink.ACTIVE:
            return
        # Check resource request timeouts (retry if no parts arrived)
        for r in self.incoming_resources:
            r.check_request_timeout()
        # Sender-side watchdog for outgoing resources: re-advertise while the
        # advertisement goes unanswered, and fail transfers that exceed the
        # overall timeout — otherwise a stalled voice/image transfer hangs
        # forever and wedges the link that later DIRECT sends reuse.
        for r in list(self.outgoing_resources):
            r.check_adv_timeout()
            if r.is_timed_out():
                r.cancel()
        # LRRTT resend: until the peer sends anything we can decrypt, its side
        # of the link may still be half-established (LRRTT lost on air — the
        # relay's radio is deaf while it transmits). A reference peer in that
        # state answers raw keepalives but silently drops every resource
        # advertisement, so without this the link wedges. Re-sending is safe:
        # each send re-encrypts (fresh IV, no dedup drop) and a peer that is
        # already established just re-fires its idempotent established-callback.
        if (self._lrrtt_data is not None and not self._lrrtt_confirmed
                and self._lrrtt_resends < 4
                and time.time() - self._lrrtt_last >= 4):
            self.send(self._lrrtt_data, const.CTX_LRRTT)
            self._lrrtt_resends += 1
            self._lrrtt_last = time.time()
            log("OutLink " + self.link_id.hex()[:8] + " LRRTT resend "
                + str(self._lrrtt_resends) + "/4", LOG_DEBUG)
        # Initiator keepalive: the peer stales the link if it hears nothing for
        # a while, so send a 0xFF probe when idle (it replies 0xFE, refreshing
        # last_activity). Only the initiator sends these (reference RNS).
        now = time.time()
        kival = self._keepalive_interval()
        if (now - self.last_activity >= kival
                and now - self._last_keepalive >= kival):
            self._send_keepalive()
            self._last_keepalive = now
        # Expire requests still waiting for a response packet/advertisement.
        # RECEIVING entries are governed by the resource retry machinery.
        if self.pending_requests:
            now = time.time()
            expired = None
            for rid in self.pending_requests:
                e = self.pending_requests[rid]
                if e[_PR_STATE] == OutgoingLink.REQ_SENT and now - e[_PR_SENT_AT] > e[_PR_TIMEOUT]:
                    if expired is None:
                        expired = []
                    expired.append(rid)
            if expired:
                for rid in expired:
                    self._fail_request(rid, "timeout")
        if time.time() - self.last_activity > 720:  # STALE_GRACE
            log("OutLink " + self.link_id.hex()[:8] + " stale, closing", LOG_VERBOSE)
            self._close()

    def check_timeout(self):
        if self.status == OutgoingLink.PENDING:
            if time.time() - self.request_time > self.establishment_timeout:
                log("OutLink " + self.link_id.hex()[:8] + " establishment timeout", LOG_VERBOSE)
                self._close()

    def teardown(self):
        """Gracefully close this link (sends close notification)."""
        if self.status == OutgoingLink.ACTIVE:
            try:
                self.send(self.link_id, const.CTX_LINKCLOSE)
            except:
                pass
        self._close()

    def _close(self):
        if self.status != OutgoingLink.CLOSED:
            self.status = OutgoingLink.CLOSED
            log("OutLink " + self.link_id.hex()[:8] + " closed", LOG_VERBOSE)
            # Fail in-flight requests first so requesters see the failure
            # before the link-closed notification.
            if self.pending_requests:
                for rid in list(self.pending_requests):
                    self._fail_request(rid, "link closed")
            if self.closed_callback:
                try:
                    self.closed_callback(self)
                except:
                    pass
        # Break circular refs (MicroPython GC can't collect cycles)
        if self._channel is not None:
            try:
                self._channel.shutdown()
            except Exception:
                pass
            self._channel = None
        self.destination = None
        self.established_callback = None
        self.closed_callback = None
        self.packet_callback = None
        self.resource_concluded_callback = None
        self.resource_started_callback = None
        for r in self.incoming_resources:
            r.link = None
        for r in self.outgoing_resources:
            r.link = None
        self.incoming_resources = []
        self.outgoing_resources = []

    def __repr__(self):
        states = {0: "PENDING", 1: "ACTIVE", 2: "CLOSED"}
        return "<OutLink:" + self.link_id.hex()[:8] + " " + states.get(self.status, "?") + ">"
