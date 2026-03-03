# µReticulum Link
# Server-side stateful encrypted link (Reticulum Link protocol)
# Supports the 3-packet ECDH handshake + request/response RPC

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


class Link:
    PENDING = 0x00
    ACTIVE  = 0x01
    CLOSED  = 0x02

    KEEPALIVE_INTERVAL  = 360   # seconds
    STALE_GRACE         = 720   # seconds
    ESTABLISHMENT_TIMEOUT = 25  # seconds (extra margin for slow ECDH on ESP32)
    CREATION_COOLDOWN   = 15    # min seconds between link creations (ESP32: ECDH ~5s)
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
            link_mtu, link_mode = _parse_signalling(raw_sig_bytes)
            self._signalling_bytes = _signalling_bytes(link_mtu, link_mode)
        else:
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
        self.status = Link.PENDING
        self.activated_at = None
        self.last_activity = time.time()
        self.last_proof_time = time.time()
        self._callbacks_fired = False

        log("Link request on " + destination.hexhash[:8] + " link_id=" + self.link_id.hex()[:8], LOG_VERBOSE)

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
            create_receipt=False,
        )
        proof_packet.send()

        # Clean up (no longer needed after proof)
        del self._ephemeral_pub_bytes, self._signalling_bytes
        gc.collect()

    def receive(self, packet):
        """Handle incoming data packet on this link."""
        try:
            plaintext = self._token.decrypt(packet.data)
        except Exception as e:
            log("Link " + self.link_id.hex()[:8] + " decrypt failed: " + str(e), LOG_DEBUG)
            return

        self.last_activity = time.time()

        if packet.context == const.CTX_LRRTT:
            self._handle_rtt(plaintext)
        elif packet.context == const.CTX_REQUEST:
            self._handle_request(plaintext, packet)
        elif packet.context == const.CTX_KEEPALIVE:
            log("Link " + self.link_id.hex()[:8] + " keepalive", LOG_DEBUG)
        elif packet.context == const.CTX_LINKCLOSE:
            log("Link " + self.link_id.hex()[:8] + " close received", LOG_VERBOSE)
            self.status = Link.CLOSED
        elif packet.context == const.CTX_LINKIDENTIFY:
            log("Link " + self.link_id.hex()[:8] + " identify (not implemented)", LOG_DEBUG)
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
            log("Link " + self.link_id.hex()[:8] + " response too large (" + str(len(response_packed)) + "B), needs Resource (not implemented)", LOG_ERROR)
            return

        self.send(response_packed, const.CTX_RESPONSE)
        log("Link " + self.link_id.hex()[:8] + " response sent (" + str(len(response_packed)) + "B)", LOG_DEBUG)

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
        packet.send()

    def check_keepalive(self):
        """Check link staleness and send keepalive if needed."""
        now = time.time()

        # Check establishment timeout for pending links
        if self.status == Link.PENDING:
            if now - self.last_proof_time > Link.ESTABLISHMENT_TIMEOUT:
                log("Link " + self.link_id.hex()[:8] + " establishment timeout", LOG_VERBOSE)
                self.status = Link.CLOSED
            return

        if self.status != Link.ACTIVE:
            return

        # Check stale grace period
        if now - self.last_activity > Link.STALE_GRACE:
            log("Link " + self.link_id.hex()[:8] + " stale, closing", LOG_VERBOSE)
            self.teardown()

    def teardown(self):
        """Close this link."""
        if self.status != Link.CLOSED:
            self.status = Link.CLOSED
            log("Link " + self.link_id.hex()[:8] + " torn down", LOG_VERBOSE)

    def __repr__(self):
        states = {0: "PENDING", 1: "ACTIVE", 2: "CLOSED"}
        return "<Link:" + self.link_id.hex()[:8] + " " + states.get(self.status, "?") + ">"
