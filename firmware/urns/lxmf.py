# µReticulum LXMF - Lightweight Extensible Message Format
# Wire-compatible with reference LXMF for MeshChat/Sideband interop
# Supports opportunistic (single-packet) and direct (link) message delivery

import time, sys, random
from . import umsgpack
from .identity import Identity
from .destination import Destination
from .packet import Packet
from .transport import Transport
from .log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE, LOG_INFO, LOG_WARNING
from . import const
from .crypto.hashes import sha256
from .crypto import ed25519

# MicroPython on ESP32 counts from 2000-01-01; LXMF timestamps and ticket
# expiries are Unix time (https://docs.micropython.org/en/latest/library/time.html).
_UNIX_EPOCH_OFFSET = 946684800 if sys.platform == "esp32" else 0


def _unix_now():
    return time.time() + _UNIX_EPOCH_OFFSET

APP_NAME = "lxmf"

# Standard LXMF fields
FIELD_EMBEDDED_LXMS    = 0x01
FIELD_TELEMETRY        = 0x02
FIELD_TELEMETRY_STREAM = 0x03
FIELD_ICON_APPEARANCE  = 0x04
FIELD_FILE_ATTACHMENTS = 0x05
FIELD_IMAGE            = 0x06
FIELD_AUDIO            = 0x07
FIELD_THREAD           = 0x08
FIELD_COMMANDS         = 0x09
FIELD_RESULTS          = 0x0A
FIELD_GROUP            = 0x0B
FIELD_TICKET           = 0x0C
FIELD_EVENT            = 0x0D
FIELD_RNR_REFS         = 0x0E
FIELD_RENDERER         = 0x0F
FIELD_REPLY_TO         = 0x30
FIELD_REPLY_QUOTE      = 0x31
FIELD_REACTION         = 0x40
FIELD_COMMENT          = 0x41
FIELD_CONTINUATION     = 0x42
# For embedding/tunnelling non-native data over LXMF
FIELD_CUSTOM_TYPE      = 0xFB
FIELD_CUSTOM_DATA      = 0xFC
FIELD_CUSTOM_META      = 0xFD
# Development / debug fields
FIELD_NON_SPECIFIC     = 0xFE
FIELD_DEBUG            = 0xFF
# (Upstream also defines AM_* audio modes and PN_META_* propagation-node
#  metadata keys; omitted here as unused — we do neither audio nor PN hosting.)

# FIELD_RENDERER values — hint to the receiver on how to render content.
RENDERER_PLAIN         = 0x00
RENDERER_MICRON        = 0x01
RENDERER_MARKDOWN      = 0x02
RENDERER_BBCODE        = 0x03

# Dict keys for FIELD_REACTION / FIELD_COMMENT / FIELD_CONTINUATION contents.
REACTION_TO            = 0x00
REACTION_CONTENT       = 0x01
COMMENT_FOR            = 0x00
CONTINUATION_OF        = 0x00

# Supported-functionality signalling codes carried in announce app_data
# element [2] (LXMF 0.9.5+). SF_COMPRESSION advertises that we accept
# bz2-compressed Resources — see LXMRouter._build_app_data.
SF_COMPRESSION         = 0x00


class LXMessage:
    """LXMF message - wire-compatible with reference implementation"""

    # States
    GENERATING  = 0x00
    OUTBOUND    = 0x01
    SENDING     = 0x02
    SENT        = 0x04
    DELIVERED   = 0x08
    REJECTED    = 0xFD     # the receiver refused the transfer: final
    CANCELLED   = 0xFE
    FAILED      = 0xFF

    # Delivery methods
    UNKNOWN       = 0x00
    OPPORTUNISTIC = 0x01
    DIRECT        = 0x02
    PROPAGATED    = 0x03

    # Representation
    PACKET   = 0x01
    RESOURCE = 0x02

    # Verification
    SOURCE_UNKNOWN    = 0x01
    SIGNATURE_INVALID = 0x02

    # A packed DIRECT message up to this size goes as one link packet (~415 B
    # after Token encryption); larger ones as a Resource.
    LINK_PACKET_MAX_PACKED = 415

    # Sizes
    DESTINATION_LENGTH = Identity.TRUNCATED_HASHLENGTH // 8   # 16
    SIGNATURE_LENGTH   = Identity.SIGLENGTH // 8              # 64
    TIMESTAMP_SIZE     = 8
    TICKET_LENGTH      = 16   # reference LXMessage.TICKET_LENGTH (truncated hash size)
    STRUCT_OVERHEAD    = 8
    LXMF_OVERHEAD      = 2 * DESTINATION_LENGTH + SIGNATURE_LENGTH + TIMESTAMP_SIZE + STRUCT_OVERHEAD  # 112

    # Max content that fits in a single encrypted packet (opportunistic): 287
    # bytes. Reference LXMF >= 1.2 dropped the "+ TIMESTAMP_SIZE" it used to
    # add here (295): that size only fits a HEADER_1 packet and overflows the
    # MTU once a transport node's id is inserted (HEADER_2), i.e. on any
    # routed path.
    ENCRYPTED_PACKET_MAX_CONTENT = Packet.ENCRYPTED_MDU - LXMF_OVERHEAD + DESTINATION_LENGTH

    def __init__(self, destination=None, source=None, content=b"", title=b"",
                 fields=None, desired_method=None,
                 destination_hash=None, source_hash=None):

        self._destination = destination
        self._source = source
        self.destination_hash = destination_hash or (destination.hash if destination else None)
        self.source_hash = source_hash or (source.hash if source else None)

        if isinstance(title, str):
            title = title.encode("utf-8")
        if isinstance(content, str):
            content = content.encode("utf-8")

        self.title = title
        self.content = content
        self.fields = fields if fields is not None else {}

        self.timestamp = None
        self.signature = None
        self.hash = None
        self.message_id = None
        self.packed = None
        # Stamp (5th payload element). Preferred: a TICKET stamp,
        # truncated_hash(ticket + message_id), free to compute, for peers
        # that handed us a ticket. Otherwise, when the peer announces a stamp
        # cost, a proof-of-work stamp in the non-expanded form LXMF >= 1.3.1
        # accepts: 32 bytes with sha256(message_id + stamp) <= target(cost).
        # (The older expanded-workblock form is not generated.)
        self.stamp = None
        self.outbound_ticket = None
        self.stamp_cost = None
        self.state = LXMessage.GENERATING
        self.method = LXMessage.UNKNOWN
        self.desired_method = desired_method

        self.incoming = False
        self.signature_validated = False
        self.unverified_reason = None
        self.transport_encrypted = False
        self.transport_encryption = None

        self.rssi = None
        self.snr = None
        self.q = None

        self._delivery_callback = None
        self._failed_callback = None
        self._sent_callback = None

        # Outbound scheduling state (reference LXMessage / LXMRouter).
        self.progress = 0.0
        self.delivery_attempts = 0
        self.sent_on_path = False
        self.sent_on_slow_path = False
        self.awaiting_path = False
        self.max_delivery_attempts = None
        self.next_delivery_attempt = None   # None: never scheduled (always due)
        self.receipt_timeout = None
        self.path_request_retried = False
        self.representation = LXMessage.UNKNOWN
        self.concluded_callback = None
        self._delivery_link = None
        self.resource_representation = None
        self._pow_pending = False
        self._sent_fired = False
        self._receipts = []         # kept opportunistic receipts, see _keep_receipt
        self.unsent_ticks = 0       # consecutive opportunistic sends that left nothing on the wire
        self._router = None

    @property
    def destination(self):
        return self._destination

    @destination.setter
    def destination(self, d):
        if self._destination is None:
            self._destination = d
        else:
            raise ValueError("Cannot reassign destination")

    @property
    def source(self):
        return self._source

    @source.setter
    def source(self, s):
        if self._source is None:
            self._source = s
        else:
            raise ValueError("Cannot reassign source")

    def content_as_string(self):
        try:
            return self.content.decode("utf-8")
        except Exception:
            return None

    def title_as_string(self):
        try:
            return self.title.decode("utf-8")
        except Exception:
            return None

    def register_delivery_callback(self, callback):
        """callback(message): the receiver's proof arrived (state DELIVERED)."""
        self._delivery_callback = callback

    def register_failed_callback(self, callback):
        """callback(message): the retry sequence is over (FAILED / REJECTED /
        CANCELLED)."""
        self._failed_callback = callback

    def register_sent_callback(self, callback):
        """callback(message): first transmission (the packet or the resource
        advertisement left this node). NOT delivery."""
        self._sent_callback = callback

    def get_destination(self):
        return self._destination

    def get_source(self):
        return self._source

    # Highest announced stamp cost we will work for (2^cost hashes on average:
    # cost 8 = 256, cost 16 = 65536). Native hashcash ~30k hashes/s -> cost 16 is
    # a couple of seconds; pure Python measures ~12-15k hashes/s on an ESP32-S3
    # at 240 MHz and assume several times slower on other boards, so cost 8 (256
    # hashes, ~20 ms there, <100 ms on a slow board) is the most it may be asked
    # to find. Above the cap the message goes out unstamped, as before.
    MAX_POW_STAMP_COST = 16         # with native hashcash
    MAX_POW_STAMP_COST_PY = 8       # pure Python
    # Up to this cost the search is a few tens of ms at most and runs inline;
    # more than that is done in event-loop slices (LXMRouter.send_message).
    INLINE_POW_STAMP_COST = 6

    @staticmethod
    def pow_stamp_cap():
        try:
            from . import stamps
            if stamps.have_hashcash():
                return LXMessage.MAX_POW_STAMP_COST
        except Exception:
            pass
        return LXMessage.MAX_POW_STAMP_COST_PY

    def _pow_stamp(self, cost):
        """Non-expanded LXMF stamp for this message, or None. Synchronous: only
        for small work (see INLINE_POW_STAMP_COST) or when no loop is running."""
        try:
            if cost > LXMessage.pow_stamp_cap():
                log("LXMF stamp cost " + str(cost) + " too high, sending unstamped", LOG_WARNING)
                return None
            from . import stamps
            target = stamps.target_from_fp(stamps.cost_to_fp(cost))
            limit = (1 << int(cost)) * 24 + 4096
            tried = 0
            while tried < limit:
                stamp, n = stamps.find_hashcash(self.message_id, target, 32)
                tried += max(n, 1)
                if stamp is not None:
                    log("LXMF stamp (cost " + str(cost) + ") found after " + str(tried) + " tries", LOG_VERBOSE)
                    return stamp
            log("LXMF stamp not found within budget, sending unstamped", LOG_WARNING)
        except Exception as e:
            log("LXMF stamp error: " + str(e), LOG_ERROR)
        return None

    async def _pow_stamp_async(self, cost, max_s=60):
        """Same stamp, found in duty-cycled slices so the event loop keeps
        running; None when over budget (the message then goes out unstamped)."""
        import uasyncio as asyncio
        from . import stamps
        stamps.interactive_enter()
        try:
            target = stamps.target_from_fp(stamps.cost_to_fp(cost))
            limit = (1 << int(cost)) * 24 + 4096
            duty = stamps.Duty(stamps.DUTY_INTERACTIVE, stamps.GRACE_MS)
            # Budget on the tick source: a wall-clock sync during the search must
            # neither end it early nor extend it.
            t0 = const.ticks_ms()
            tried = 0
            while tried < limit and const.ticks_diff(const.ticks_ms(), t0) < max_s * 1000:
                duty.begin()
                stamp, n = stamps.find_hashcash(self.message_id, target, 32)
                tried += max(n, 1)
                if stamp is not None:
                    log("LXMF stamp (cost " + str(cost) + ") found after " + str(tried) + " tries", LOG_VERBOSE)
                    return stamp
                await asyncio.sleep_ms(duty.rest_ms())
            log("LXMF stamp not found within budget, sending unstamped", LOG_WARNING)
        except Exception as e:
            log("LXMF stamp error: " + str(e), LOG_ERROR)
        finally:
            stamps.interactive_exit()
        return None

    def _pow_deferrable(self, cost):
        return LXMessage.INLINE_POW_STAMP_COST < cost <= LXMessage.pow_stamp_cap()

    def pack(self, defer_pow=False):
        """Pack message into wire format. With defer_pow=True a proof-of-work
        stamp that is too expensive to search inline is NOT searched: the
        message is packed unstamped, True is returned, and the caller must run
        _pow_stamp_async() and finish_pow() before sending."""
        if self.packed:
            raise ValueError("Message already packed")

        if self.timestamp is None:
            self.timestamp = _unix_now()

        payload = [self.timestamp, self.title, self.content, self.fields]

        # Compute message hash (message_id)
        hashed_part = b""
        hashed_part += self._destination.hash
        hashed_part += self._source.hash
        hashed_part += umsgpack.packb(payload)
        self.hash = sha256(hashed_part)
        self.message_id = self.hash

        # Sign: hash(dest + source + payload) + message_hash
        signed_part = hashed_part + self.hash
        self.signature = self._source.sign(signed_part)
        try:
            import gc; gc.collect()
        except:
            pass
        self.signature_validated = True

        # Stamp, appended as the 5th payload element AFTER hashing and
        # signing (reference LXMessage.pack: hash/signature cover the
        # 4-element payload; the receiver strips element 4 before verifying).
        # A ticket stamp (free) always takes precedence over proof of work.
        pending = False
        if self.outbound_ticket:
            self.stamp = Identity.truncated_hash(self.outbound_ticket + self.message_id)
            payload.append(self.stamp)
        elif self.stamp_cost:
            if defer_pow and self._pow_deferrable(self.stamp_cost):
                pending = True
            else:
                self.stamp = self._pow_stamp(self.stamp_cost)
                if self.stamp is not None:
                    payload.append(self.stamp)
        self._payload = payload
        self._assemble(payload)
        return pending

    def finish_pow(self, stamp):
        """Complete a deferred pack(): attach the stamp (None: send unstamped)."""
        if stamp is not None:
            self.stamp = stamp
            self._assemble(self._payload + [stamp])

    def _assemble(self, payload):
        packed_payload = umsgpack.packb(payload)
        self.packed = b""
        self.packed += self._destination.hash
        self.packed += self._source.hash
        self.packed += self.signature
        self.packed += packed_payload

        # Determine delivery method
        content_size = len(packed_payload) - self.TIMESTAMP_SIZE - self.STRUCT_OVERHEAD

        if self.desired_method is None:
            self.desired_method = LXMessage.OPPORTUNISTIC

        if self.desired_method == LXMessage.OPPORTUNISTIC:
            if content_size > self.ENCRYPTED_PACKET_MAX_CONTENT:
                log("Message too large for opportunistic, using DIRECT", LOG_DEBUG)
                self.desired_method = LXMessage.DIRECT

        self.method = self.desired_method
        if self.method == LXMessage.DIRECT and len(self.packed) > LXMessage.LINK_PACKET_MAX_PACKED:
            self.representation = LXMessage.RESOURCE
        else:
            self.representation = LXMessage.PACKET

    def send(self):
        """Transmit ONE delivery attempt (reference LXMessage.send). The router
        decides when; the packed bytes are the same on every attempt. For
        DIRECT the router has set _delivery_link (an ACTIVE link). State after:
        OPPORTUNISTIC -> SENT, DIRECT -> SENDING; DELIVERED arrives with the
        receiver's proof (_mark_delivered)."""
        if not self.packed:
            self.pack()
        from .packet import PacketReceipt

        if self.method == LXMessage.OPPORTUNISTIC:
            # The destination hash is inferred from the packet header
            pkt = Packet(self._destination, self.packed[self.DESTINATION_LENGTH:])
            receipt = pkt.send()
            if receipt is False:
                # Nothing left the node (no interface online / every interface refused it).
                # Not SENT: no sent callback, no "sent on path" claim; the router retries.
                log("Opportunistic LXMF message to " + self.destination_hash.hex()[:8]
                    + " was not transmitted (no interface could send)", LOG_WARNING)
                return False
            self.progress = 0.50
            self.state = LXMessage.SENT
            self.transport_encrypted = True
            self.transport_encryption = "Curve25519"
            log("Sent opportunistic LXMF message to " + self.destination_hash.hex()[:8], LOG_NOTICE)
            self._mark_sent()
            if receipt:
                receipt.set_delivery_callback(self._mark_delivered)
                self._keep_receipt(receipt)
                if self.receipt_timeout:
                    receipt.set_timeout(max(receipt.timeout, self.receipt_timeout))
                if receipt.status == PacketReceipt.DELIVERED:
                    self._mark_delivered(receipt)
            else:
                log("No receipt generated on sent opportunistic LXMF message", LOG_WARNING)

        elif self.method == LXMessage.DIRECT:
            self.state = LXMessage.SENDING
            link = self._delivery_link
            self.transport_encrypted = True
            self.transport_encryption = "Curve25519"
            if self.representation == LXMessage.PACKET:
                receipt = link.send(self.packed, const.CTX_NONE, create_receipt=True)
                if receipt:
                    self.progress = 0.50
                    receipt.set_delivery_callback(self._mark_delivered)
                    receipt.set_timeout_callback(self._link_packet_timed_out)
                    log("LXMF DIRECT sent as packet: " + str(len(self.packed)) + "B", LOG_VERBOSE)
                    self._mark_sent()
                    if receipt.status == PacketReceipt.DELIVERED:
                        self._mark_delivered(receipt)
                else:
                    link.teardown()
                    self.state = LXMessage.OUTBOUND
            else:
                from .resource import Resource
                res = Resource(link, self.packed, is_response=False)
                res.lxm = self
                self.resource_representation = res
                self.progress = 0.10
                log("LXMF DIRECT sending as resource: " + str(len(self.packed)) + "B", LOG_VERBOSE)
                self._mark_sent()
        else:
            log("Unsupported delivery method: " + str(self.method), LOG_ERROR)
            self.state = LXMessage.FAILED
        return True

    KEEP_RECEIPTS = 3           # receipts of earlier attempts that stay matchable (RAM bound per message)

    def _keep_receipt(self, receipt):
        """A proof for ANY transmission of this message counts for as long as the message is
        pending: a slow (pure-Python) receiver's proof for attempt 1 may arrive after attempt
        2 went out. Keeps the last KEEP_RECEIPTS receipts in Transport.receipts even after
        they time out; _release_receipts() lets them go when the message concludes."""
        from .transport import Transport
        receipt.keep = True
        self._receipts.append(receipt)
        while len(self._receipts) > self.KEEP_RECEIPTS:
            self._drop_receipt(self._receipts.pop(0), Transport)

    @staticmethod
    def _drop_receipt(r, Transport):
        r.keep = False
        if r.status != 1 and r in Transport.receipts:       # already concluded: no job-loop sweep will take it
            Transport.receipts.remove(r)

    def _release_receipts(self):
        from .transport import Transport
        while self._receipts:
            self._drop_receipt(self._receipts.pop(), Transport)

    def _mark_sent(self):
        if self._sent_fired:
            return
        self._sent_fired = True
        for cb in (self._sent_callback, self._router._sent_cb if self._router else None):
            if cb is not None:
                try:
                    cb(self)
                except Exception as e:
                    log("LXMF sent callback error: " + str(e), LOG_ERROR)

    def _mark_delivered(self, receipt=None):
        if self.state == LXMessage.DELIVERED:
            return
        self.state = LXMessage.DELIVERED
        self.progress = 1.0
        if receipt is not None and self.method == LXMessage.OPPORTUNISTIC and self._router is not None:
            ms = getattr(receipt, "_sent_ms", None)
            if ms is not None:
                try:
                    self._router.note_proof_latency(self.destination_hash,
                                                    const.ticks_diff(const.ticks_ms(), ms) / 1000)
                except Exception:
                    pass
        log("LXMF delivery proof received for " + self.destination_hash.hex()[:8], LOG_NOTICE)
        if self.concluded_callback is not None:
            try:
                self.concluded_callback(self)
            except Exception as e:
                log("LXMF concluded callback error: " + str(e), LOG_ERROR)
        for cb in (self._delivery_callback, self._router._delivered_cb if self._router else None):
            if cb is not None:
                try:
                    cb(self)
                except Exception as e:
                    log("Delivery callback error: " + str(e), LOG_ERROR)

    def _resource_concluded(self, resource):
        from .resource import COMPLETE, REJECTED
        if resource.status == COMPLETE:
            self._mark_delivered()
        elif resource.status == REJECTED:
            self.state = LXMessage.REJECTED
        elif self.state != LXMessage.CANCELLED:
            link = self._delivery_link or getattr(resource, "link", None)
            if link is not None:
                link.teardown()
            self.state = LXMessage.OUTBOUND

    def _link_packet_timed_out(self, receipt):
        if self.state != LXMessage.CANCELLED:
            if self._delivery_link is not None:
                self._delivery_link.teardown()
            self.state = LXMessage.OUTBOUND

    @staticmethod
    def unpack_from_bytes(lxmf_bytes):
        """Unpack LXMF message from wire format bytes"""
        DL = LXMessage.DESTINATION_LENGTH
        SL = LXMessage.SIGNATURE_LENGTH

        destination_hash = lxmf_bytes[:DL]
        source_hash = lxmf_bytes[DL:2 * DL]
        signature = lxmf_bytes[2 * DL:2 * DL + SL]
        packed_payload = lxmf_bytes[2 * DL + SL:]

        unpacked_payload = umsgpack.unpackb(packed_payload)

        # Extract stamp if present (5th element)
        stamp = None
        if len(unpacked_payload) > 4:
            stamp = unpacked_payload[4]
            unpacked_payload = unpacked_payload[:4]
            packed_payload = umsgpack.packb(unpacked_payload)

        # Compute message hash
        hashed_part = destination_hash + source_hash + packed_payload
        message_hash = sha256(hashed_part)
        signed_part = hashed_part + message_hash

        timestamp = unpacked_payload[0]
        title_bytes = unpacked_payload[1]
        content_bytes = unpacked_payload[2]
        fields = unpacked_payload[3]

        # Try to find source identity for signature validation
        source_identity = Identity.recall(source_hash)

        # Build source destination if identity known
        source_dest = None
        if source_identity:
            source_dest = Destination(source_identity, Destination.OUT,
                                       Destination.SINGLE, APP_NAME, "delivery")

        # Build destination object if identity known
        dest_identity = Identity.recall(destination_hash)
        dest_obj = None
        if dest_identity:
            dest_obj = Destination(dest_identity, Destination.OUT,
                                    Destination.SINGLE, APP_NAME, "delivery")

        message = LXMessage(
            destination=dest_obj,
            source=source_dest,
            destination_hash=destination_hash,
            source_hash=source_hash,
        )

        message.hash = message_hash
        message.message_id = message_hash
        message.signature = signature
        message.stamp = stamp
        message.incoming = True
        message.timestamp = timestamp
        message.title = title_bytes if isinstance(title_bytes, bytes) else title_bytes.encode("utf-8") if title_bytes else b""
        message.content = content_bytes if isinstance(content_bytes, bytes) else content_bytes.encode("utf-8") if content_bytes else b""
        message.fields = fields if isinstance(fields, dict) else {}
        message.packed = lxmf_bytes

        # Validate signature
        if source_identity:
            if not LXMRouter.verify_signatures:
                # Skip expensive Ed25519 verify on constrained devices.
                # Message is already authenticated by encryption layer
                # (X25519 ECDH + HMAC-SHA256).
                message.signature_validated = True
            else:
                try:
                    if source_identity.validate(signature, signed_part):
                        message.signature_validated = True
                    else:
                        message.signature_validated = False
                        message.unverified_reason = LXMessage.SIGNATURE_INVALID
                except Exception as e:
                    message.signature_validated = False
                    log("Signature validation error: " + str(e), LOG_DEBUG)
        else:
            message.signature_validated = False
            message.unverified_reason = LXMessage.SOURCE_UNKNOWN

        # Bootstrap the clock from a trusted, signature-validated sender.
        # No-op unless time sync is enabled and the clock is still unset.
        if message.signature_validated:
            try:
                from .transport import Transport
                Transport.mark_in_use(source_hash)
                Transport.sync_clock_from(timestamp, source_hash)
            except Exception:
                pass

        return message

    def __str__(self):
        if self.hash:
            return "<LXMessage " + self.hash.hex()[:8] + ">"
        return "<LXMessage>"


class LXMRouter:
    """Simplified LXMF router for µReticulum
    
    Handles:
    - Registering delivery identity (makes node visible in MeshChat)
    - Receiving opportunistic LXMF messages
    - Sending opportunistic LXMF messages
    - Peer announce tracking
    """

    # Verify sender signatures when the native Ed25519 module makes it cheap
    # (milliseconds). Pure Python verify costs ~2s/message on ESP32, so fall
    # back to trusting the encryption layer (X25519 ECDH + HMAC-SHA256) there.
    verify_signatures = ed25519.have_native()

    def __init__(self, identity=None, storagepath=None):
        self.identity = identity
        # Default to the node's storage dir (Reticulum sets Identity.storagepath
        # at init, e.g. "/rns") so ticket persistence works in every app
        # without plumbing a path through.
        if storagepath is None:
            storagepath = getattr(Identity, "storagepath", None)
        self.storagepath = storagepath

        self.delivery_destination = None
        self.delivery_identity = None
        self.display_name = None

        self._delivery_callback = None
        self._announce_callback = None
        self._progress_callback = None

        # Outbound delivery state (see "Outbound delivery scheduling").
        self._sent_cb = None
        self._delivered_cb = None
        self._failed_cb = None
        self.pending_outbound = []
        self.direct_links = {}          # dest_hash -> OutgoingLink we opened
        self.path_request_times = {}    # dest_hash -> last debounced request
        self._slow_cache = {}
        self._processing = False
        self._again = False
        self._job_task = None

        # Tickets peers gave us (FIELD_TICKET in their messages) so we can
        # reply with a free ticket stamp: dest_hash -> [expires_unix, ticket].
        # Persisted: a reference peer re-issues a ticket at most once a day
        # (TICKET_INTERVAL), so losing it on reboot would fail replies for
        # up to a day.
        self.outbound_tickets = {}
        self._load_tickets()

        self.peers = {}  # hash -> {name, timestamp, identity}
        # Observed send-to-proof latency of opportunistic messages (seconds) per
        # peer, bounded; replaces nothing in `peers` because handle_announce
        # rewrites that entry on every announce.
        self._proof_latency = {}
        # No native Ed25519/X25519 here: a strong hint the network is made of
        # slow pure-Python boards whose receivers need seconds before they prove.
        self._slow_crypto = not ed25519.have_native()
        self.delivered_ids = {}  # message_hash -> timestamp (dedup)

        self.MESSAGE_EXPIRY = 30 * 24 * 60 * 60  # 30 days

        # A wall-clock sync must not disturb delivery: re-base our timestamps.
        if self._shift_clocks not in Transport.clock_shift_hooks:
            Transport.clock_shift_hooks.append(self._shift_clocks)

    def _shift_clocks(self, _sh):
        """Transport._shift_clocks hook: move every wall-clock stamp the router
        holds by the same rule Transport uses (_sh shifts only pre-sync values).
        Receipt timeouts and the PoW search budget run on ticks and need nothing."""
        for m in self.pending_outbound:
            if m.next_delivery_attempt is not None:
                m.next_delivery_attempt = _sh(m.next_delivery_attempt)
        for d in self.path_request_times:
            self.path_request_times[d] = _sh(self.path_request_times[d])
        for d in list(self._slow_cache):
            v = self._slow_cache[d]
            self._slow_cache[d] = (v[0], _sh(v[1]))
        for k in self.delivered_ids:
            self.delivered_ids[k] = _sh(self.delivered_ids[k])
        for p in self.peers.values():
            if isinstance(p, dict) and "timestamp" in p:
                p["timestamp"] = _sh(p["timestamp"])

    def register_delivery_identity(self, identity, display_name=None, stamp_cost=None):
        """Register identity for receiving LXMF messages.
        Creates the lxmf.delivery destination and sets up callbacks."""
        self.delivery_identity = identity
        self.display_name = display_name

        self.delivery_destination = Destination(
            identity, Destination.IN, Destination.SINGLE,
            APP_NAME, "delivery"
        )

        # Set the packet callback for incoming opportunistic messages
        self.delivery_destination.set_packet_callback(self._delivery_packet)

        # Prove EVERY opportunistic packet that decrypts (reference LXMF:
        # PROVE_ALL), duplicates and unstamped/invalid ones included. A
        # reference sender whose first proof was lost resends the same message
        # as a new packet; if only the first copy were proved it would retry
        # until it failed even though we have the message.
        self.delivery_destination.set_proof_strategy(Destination.PROVE_ALL)

        # Set link established callback for incoming link-based delivery (Resources)
        self.delivery_destination.set_link_established_callback(self._on_link_established)

        # Set announce handler so Transport forwards peer announces to us
        self.delivery_destination._announce_handler = self._announce_handler

        # Set app_data for announces. set_default_app_data so plain
        # Destination.announce() calls (path responses, post-clock-sync
        # re-announces) also carry the LXMF app_data — announce() falls back
        # to default_app_data.
        if display_name is not None:
            self.delivery_destination.set_default_app_data(
                self._build_app_data(display_name, stamp_cost))

        log("LXMF delivery registered: " + self.delivery_destination.hexhash, LOG_NOTICE)
        return self.delivery_destination

    def register_delivery_callback(self, callback):
        """Register callback for incoming messages: callback(lxmessage)"""
        self._delivery_callback = callback

    # ==================================================================
    # NOTE FOR UI INTEGRATIONS (T-Deck and others): delivery semantics changed
    # ==================================================================
    # OLD: a message's delivery callback fired when the packet was HANDED TO THE
    #      RADIO (send time). It said nothing about the peer having it, and a
    #      message to an unknown/offline peer showed up as "sent" forever.
    # NEW: send_message() queues the message and returns the LXMessage (or None
    #      when the queue is full). It then moves OUTBOUND -> SENT (opportunistic)
    #      or SENDING (link) -> DELIVERED, which happens ONLY when the
    #      receiver's cryptographic proof arrives. Three callbacks, per message
    #      (send_message(..._callback=) or msg.register_*_callback) and router-
    #      wide (register_outbound_callbacks):
    #        sent(msg)       first transmission (what "delivery" used to mean)
    #        delivered(msg)  the proof arrived, state == DELIVERED, fires once
    #        failed(msg)     the retry sequence ended: state FAILED (no proof
    #                        after all attempts, or no path/identity), REJECTED
    #                        (peer refused a transfer, no retry) or CANCELLED
    #      What a UI should show: OUTBOUND = "queued" (waiting for a path or the
    #      next attempt), SENT/SENDING = "sent" (one tick, not yet confirmed,
    #      msg.progress 0..1 tracks a large transfer), DELIVERED = "delivered"
    #      (double tick), FAILED/REJECTED/CANCELLED = "failed" with a retry
    #      button (a retry is a NEW send_message()). Do not treat the old
    #      callback as proof of delivery; do not block the UI waiting for it.
    #      Timing: delivery normally confirms within one round trip of the send.
    #      A message nobody confirms is resent (>= 10 s apart), its path is
    #      rediscovered after the 2nd send, and it fails after ~56 s (path known)
    #      or ~60 s (no path) on a fast link or SF7/125 kHz LoRa (up to ~68/84 s
    #      with SF7 airtime jitter), 144-240 s on a 300 bit/s link, ~32 s for a
    #      DIRECT message with no path. docs/lxmf-delivery-semantics.md has the
    #      retry sequence and the full timing table.
    #      DIRECT links are closed as soon as nothing queued needs them (the
    #      reference holds them 10 min; return-channel handling on links we
    #      opened is not implemented, see docs/lxmf-delivery-semantics.md).
    #      Queue full: send_message() returns None and logs a warning; at most
    #      MAX_PENDING_OUTBOUND (8 with plenty of heap, else 4) messages are
    #      queued, none is ever dropped silently. Show "busy, try again".
    #      send_message() no longer returns None for an unknown identity: it
    #      asks for a path and fails later via the failed callback.
    #      msg.message_id (and .hash) is None until the peer's identity is known
    #      and the message is built; cancel_outbound() needs it.
    #      The router-wide inbound callback (register_delivery_callback) is
    #      unchanged: it still means "a message ARRIVED".
    # ==================================================================
    def register_outbound_callbacks(self, sent=None, delivered=None, failed=None):
        """Router-wide outbound callbacks, each callback(LXMessage); None leaves
        a slot unchanged. See the NOTE FOR UI INTEGRATIONS above."""
        if sent is not None:
            self._sent_cb = sent
        if delivered is not None:
            self._delivered_cb = delivered
        if failed is not None:
            self._failed_cb = failed

    def register_sent_callback(self, callback):
        self._sent_cb = callback

    def register_delivered_callback(self, callback):
        self._delivered_cb = callback

    def register_failed_callback(self, callback):
        self._failed_cb = callback

    def register_announce_callback(self, callback):
        """Register callback for peer announces: callback(destination_hash, display_name)"""
        self._announce_callback = callback

    def register_progress_callback(self, callback):
        """Register callback for incoming resource transfer progress:
        callback(resource) — reference RNS Resource progress_callback
        signature; read resource.get_progress() or received_count/
        total_parts on it."""
        self._progress_callback = callback

    def announce(self):
        """Announce our LXMF delivery destination"""
        if self.delivery_destination:
            app_data = self._get_announce_app_data()
            ok = self.delivery_destination.announce(app_data=app_data)
            if ok:
                log("LXMF announced as: " + (self.display_name or "unnamed"), LOG_NOTICE)
            else:
                log("LXMF announce NOT sent: no interface could transmit it", LOG_ERROR)
            return ok

    def _get_announce_app_data(self):
        """Build announce app_data for our delivery destination.
        Leaf nodes never require a stamp, so stamp_cost is None."""
        return self._build_app_data(self.display_name, None)

    @staticmethod
    def _build_app_data(display_name, stamp_cost=None):
        """Build LXMF announce app_data: msgpack([display_name_bytes|None,
        stamp_cost, supported_functionality]) — the upstream LXMF shape.

        We advertise SF_COMPRESSION in the functionality list: our Resource
        layer decompresses inbound bz2 transfers on every board (native module
        or the pure-Python fallback in bz2dec), so peers may compress large
        Resources sent to us, saving airtime. Older peers that read only
        elements [0]/[1] are unaffected."""
        dn = None
        if display_name:
            dn = display_name.encode("utf-8") if isinstance(display_name, str) else display_name
        return umsgpack.packb([dn, stamp_cost, [SF_COMPRESSION]])

    # ------------------------------------------------------------------
    # Outbound delivery scheduling (reference LXMF 1.3.1 LXMRouter).
    #
    # send_message() builds an LXMessage and hands it to handle_outbound(),
    # which queues it on pending_outbound. process_outbound() (a job every
    # PROCESSING_INTERVAL seconds, and immediately on submit / link
    # establishment / slow-path conclusion) walks the queue and drives each
    # message through OUTBOUND -> SENDING/SENT -> DELIVERED, or FAILED /
    # CANCELLED / REJECTED, with the reference's attempt counts and timings.
    # Port differences (single-threaded uasyncio, RAM bounds) are marked
    # "PORT:" below.
    # ------------------------------------------------------------------

    MAX_DELIVERY_ATTEMPTS  = 5
    PROCESSING_INTERVAL    = 4
    MAX_UNSENT_TICKS       = 30    # ~2 min of ticks in which nothing could be transmitted: give up
    DELIVERY_RETRY_WAIT    = 10
    PATH_REQUEST_WAIT      = const.PATH_REQUEST_WAIT        # 7
    MAX_PATHLESS_TRIES     = 1
    PATH_REQUEST_DEBOUNCE  = const.PATH_REQUEST_DEBOUNCE    # 60
    SLOW_INTERFACE_BITRATE = const.SLOW_INTERFACE_BITRATE   # 2000
    AIRTIME_AWARE_MTU_TIME = 0.5
    # PORT: queue bound (the reference is unbounded) follows the heap rule
    # const.py uses for its table sizes. DIRECT links are NOT held idle (the
    # reference keeps them 10 min): see _release_link().
    MAX_PENDING_OUTBOUND   = 8 if const._BIG_RAM else 4
    _SLOW_CACHE_MAX        = 16      # reference 256
    _PATH_REQ_TIMES_MAX    = 16      # unbounded in the reference

    # -- timing helpers ------------------------------------------------

    def medium_path_timeout(self):
        try:
            return Transport.medium_path_timeout()
        except Exception:
            return 0

    def destination_round_trip(self, destination_hash):
        if not Transport.has_path(destination_hash):
            return self.medium_path_timeout()
        try:
            mtu_time = Transport.first_hop_timeout(destination_hash) - const.DEFAULT_PER_HOP_TIMEOUT
            return 2 * max(mtu_time, 0) + const.DEFAULT_PER_HOP_TIMEOUT
        except Exception:
            return 0

    def path_request_wait(self):
        return max(LXMRouter.PATH_REQUEST_WAIT, self.medium_path_timeout())

    def slow_interface_online(self):
        try:
            return Transport.slow_interface_online()
        except Exception:
            return False

    def request_path(self, destination_hash):
        """Debounced path request (reference LXMRouter.request_path): no second
        request for the same destination inside path_request_wait() (60 s while
        a slow interface is online). True when a request was sent."""
        if self.slow_interface_online():
            window = LXMRouter.PATH_REQUEST_DEBOUNCE
        else:
            window = self.path_request_wait()
        now = time.time()
        t = self.path_request_times.get(destination_hash)
        if t is not None and now - t < window:
            log("Path request for " + destination_hash.hex()[:8] + " still pending, not repeating within "
                + str(int(window)) + " s", LOG_DEBUG)
            return False
        times = self.path_request_times
        for d in [d for d in times if now - times[d] >= window]:
            del times[d]
        if len(times) >= LXMRouter._PATH_REQ_TIMES_MAX:
            del times[min(times, key=lambda k: times[k])]
        times[destination_hash] = now
        Transport.request_path(destination_hash)
        return True

    def path_request_pending(self, destination_hash):
        t = self.path_request_times.get(destination_hash)
        return t is not None and time.time() - t < self.path_request_wait()

    def attempt_limit(self, lxm):
        if lxm.max_delivery_attempts is None:
            return LXMRouter.MAX_DELIVERY_ATTEMPTS
        return lxm.max_delivery_attempts

    def attempt_due(self, lxm, destination_hash):
        if lxm.next_delivery_attempt is None:
            return True
        if time.time() > lxm.next_delivery_attempt:
            return True
        return lxm.awaiting_path and Transport.has_path(destination_hash)

    def schedule_attempt(self, lxm, destination_hash, path_request=False):
        if path_request:
            round_trip = self.medium_path_timeout()
            lxm.next_delivery_attempt = time.time() + max(LXMRouter.PATH_REQUEST_WAIT, round_trip)
            lxm.awaiting_path = True
        else:
            round_trip = self.destination_round_trip(destination_hash)
            wait = max(LXMRouter.DELIVERY_RETRY_WAIT, round_trip)
            if lxm.method == LXMessage.OPPORTUNISTIC:
                wait = max(wait, self.adaptive_wait(destination_hash, wait))
            lxm.next_delivery_attempt = time.time() + wait

        if round_trip > LXMRouter.DELIVERY_RETRY_WAIT:
            lxm.max_delivery_attempts = max(3, round(LXMRouter.MAX_DELIVERY_ATTEMPTS * LXMRouter.DELIVERY_RETRY_WAIT / round_trip))
        else:
            lxm.max_delivery_attempts = LXMRouter.MAX_DELIVERY_ATTEMPTS

        mtu_time = max(round_trip - const.DEFAULT_PER_HOP_TIMEOUT, 0) / 2
        if mtu_time > LXMRouter.AIRTIME_AWARE_MTU_TIME:
            lxm.next_delivery_attempt += mtu_time * random.random()      # uniform(0, mtu_time)
            retry_wait = max(LXMRouter.DELIVERY_RETRY_WAIT, round_trip) + mtu_time
            remaining = max(1, lxm.max_delivery_attempts - lxm.delivery_attempts + 1)
            lxm.receipt_timeout = retry_wait * remaining + LXMRouter.PROCESSING_INTERVAL
        else:
            lxm.receipt_timeout = None

    LATENCY_FACTOR = 1.5
    MAX_ADAPTIVE_WAIT = 120
    _LATENCY_MAX = 16

    def note_proof_latency(self, destination_hash, seconds):
        """A proof for an opportunistic message arrived `seconds` after the attempt
        it answers (measured on the tick clock by LXMessage._mark_delivered)."""
        lat = self._proof_latency
        if destination_hash not in lat and len(lat) >= LXMRouter._LATENCY_MAX:
            del lat[next(iter(lat))]
        lat[destination_hash] = seconds

    def adaptive_wait(self, destination_hash, reference_wait):
        """PORT deviation from the reference's fixed retry wait. The wait before the
        next opportunistic attempt must outlast the receiver's crypto time, or a
        pure-Python peer processes every message twice. With a recorded latency for
        this peer: LATENCY_FACTOR x the last observed send-to-proof time. Without
        one, a board that has no native crypto itself starts from the reference wait
        plus RECEIPT_PEER_CRYPTO_GRACE (the grace the receipts already use). The
        caller takes max(reference wait, this), so the reference rule is the floor."""
        lat = self._proof_latency.get(destination_hash)
        if lat is not None:
            return min(LXMRouter.LATENCY_FACTOR * lat, LXMRouter.MAX_ADAPTIVE_WAIT)
        if self._slow_crypto:
            return reference_wait + const.RECEIPT_PEER_CRYPTO_GRACE
        return 0

    def slow_path(self, destination_hash):
        now = time.time()
        cached = self._slow_cache.get(destination_hash)
        if cached and now - cached[1] < LXMRouter.PROCESSING_INTERVAL:
            return cached[0]
        if len(self._slow_cache) >= LXMRouter._SLOW_CACHE_MAX:
            self._slow_cache = {}
        slow = self.destination_round_trip(destination_hash) > LXMRouter.DELIVERY_RETRY_WAIT
        self._slow_cache[destination_hash] = (slow, now)
        return slow

    def slow_path_in_flight(self, lxm):
        if not lxm.sent_on_slow_path:
            return False
        if lxm.method == LXMessage.OPPORTUNISTIC:
            return lxm.state == LXMessage.SENT
        return lxm.state == LXMessage.SENDING

    def slow_path_held(self, lxm, target_hash):
        """A message to a slow destination waits while an earlier message to
        the same target, or ANY slow-path message, is still in flight."""
        if lxm.state != LXMessage.OUTBOUND:
            return False
        if not self.slow_path(target_hash):
            return False
        ahead = True
        for other in self.pending_outbound:
            if other is lxm:
                ahead = False
            elif other.state in (LXMessage.DELIVERED, LXMessage.FAILED,
                                 LXMessage.CANCELLED, LXMessage.REJECTED):
                continue
            elif ahead and other.destination_hash == target_hash:
                return True
            elif self.slow_path_in_flight(other):
                return True
        return False

    def outbound_concluded(self, lxm):
        """A message was delivered: leave the queue now (not at the next 4 s tick),
        free its DIRECT link when nothing else needs it, and let whatever a slow
        path was holding up go."""
        if lxm.state == LXMessage.DELIVERED and self._dequeue(lxm):
            Transport.mark_in_use(lxm.destination_hash)
            self._later(0, lambda: self._release_link(lxm.destination_hash))
        if lxm.sent_on_slow_path:
            self.process_outbound()

    # -- submit --------------------------------------------------------

    def send_message(self, destination_hash, content, title="",
                     fields=None, desired_method=None,
                     delivery_callback=None, failed_callback=None, sent_callback=None):
        """Queue an LXMF message for delivery. Returns the LXMessage (check
        .state; it is OUTBOUND/SENT/SENDING until the receiver's proof makes it
        DELIVERED) or None when it was rejected: the outbound queue is full
        (MAX_PENDING_OUTBOUND), or the message could not be built.

        An unknown destination identity or path is not a rejection: the path
        is requested and the message fails (failed callback) only after the
        reference retry sequence. See the NOTE FOR UI INTEGRATIONS above
        register_outbound_callbacks()."""
        # A conversation peer: protect its route and key from eviction by
        # unsolicited announces for a while (a busy hub announces ~8/s).
        Transport.mark_in_use(destination_hash)

        # Prefer an already-open link when the caller hasn't forced a method:
        # avoids a redundant path request when we can answer over the link.
        if desired_method is None and self._find_active_link_for(destination_hash) is not None:
            desired_method = LXMessage.DIRECT

        msg = LXMessage(destination=None, source=None, content=content, title=title,
                        fields=fields, desired_method=desired_method or LXMessage.OPPORTUNISTIC,
                        destination_hash=destination_hash)
        msg._delivery_callback = delivery_callback
        msg._failed_callback = failed_callback
        msg._sent_callback = sent_callback
        return self.handle_outbound(msg)

    def _resolve(self, msg):
        """Give a message to an identity-less destination hash its Destination
        objects, once the peer's identity is known. PORT: the reference app
        supplies the destination object; this API takes a hash."""
        if msg._destination is not None:
            return True
        ident = Identity.recall(msg.destination_hash)
        if ident is None:
            return False
        msg._destination = Destination(ident, Destination.OUT, Destination.SINGLE, APP_NAME, "delivery")
        msg._source = Destination(self.delivery_identity, Destination.OUT, Destination.SINGLE, APP_NAME, "delivery")
        msg.source_hash = msg._source.hash
        return True

    def _prepare(self, msg):
        """Stamp inputs + pack (reference handle_outbound), once the message has
        a destination. The packed bytes (and any proof-of-work stamp) are
        computed here exactly once and reused by every retry."""
        # A peer that requires stamps gave us a ticket; stamp with it so the
        # reply isn't dropped as "invalid stamp".
        msg.outbound_ticket = self.get_outbound_ticket(msg.destination_hash)
        # No ticket: fall back to a proof-of-work stamp at the cost the peer
        # announced (cheap for typical costs; see LXMessage._pow_stamp).
        if not msg.outbound_ticket and msg.stamp_cost is None:
            p = self.peers.get(msg.destination_hash)
            if p:
                msg.stamp_cost = p.get("stamp_cost")
        if msg.packed:
            return
        if msg.pack(defer_pow=True):
            # A proof-of-work stamp worth more than a few tens of ms: search it
            # in event-loop slices; the message is skipped by processing until
            # the stamp is attached.
            msg._pow_pending = True
            coro = self._pow_task(msg)
            try:
                import uasyncio as asyncio
                asyncio.create_task(coro)
            except Exception:
                coro.close()
                msg._pow_pending = False
                msg.finish_pow(msg._pow_stamp(msg.stamp_cost))   # no loop: inline

    async def _pow_task(self, msg):
        try:
            stamp = await msg._pow_stamp_async(msg.stamp_cost)
            if msg.state != LXMessage.CANCELLED:
                msg.finish_pow(stamp)
        except Exception as e:
            log("LXMF stamped send failed: " + str(e), LOG_ERROR)
            msg.state = LXMessage.FAILED
        msg._pow_pending = False
        self.process_outbound()

    def handle_outbound(self, msg):
        """Queue `msg` (reference handle_outbound). Returns msg, or None when the
        queue is full (nothing was queued; logged)."""
        self._reap_concluded()
        if len(self.pending_outbound) >= self.MAX_PENDING_OUTBOUND:
            log("LXMF outbound queue full (" + str(len(self.pending_outbound)) + "), message to "
                + msg.destination_hash.hex()[:8] + " rejected", LOG_WARNING)
            return None
        msg.state = LXMessage.OUTBOUND
        msg.concluded_callback = self.outbound_concluded
        msg._router = self
        h = msg.destination_hash

        unresolved = not self._resolve(msg)
        if not unresolved:
            try:
                self._prepare(msg)
            except Exception as e:
                log("LXMF message build failed: " + str(e), LOG_ERROR)
                msg.state = LXMessage.FAILED
                return None

        unknown_path_requested = False
        if unresolved or (not Transport.has_path(h) and msg.method == LXMessage.OPPORTUNISTIC):
            log("Pre-emptively requesting unknown path for " + h.hex()[:8], LOG_DEBUG)
            self.request_path(h)
            self.schedule_attempt(msg, h, path_request=True)
            unknown_path_requested = True

        self.pending_outbound.append(msg)
        self._ensure_job()
        if not unknown_path_requested:
            self.process_outbound()
        return msg

    # -- the queue -----------------------------------------------------

    def _reap_concluded(self):
        """Drop messages that are already over (delivered, failed, cancelled,
        rejected) from the queue: they would otherwise count against
        MAX_PENDING_OUTBOUND until the next 4 s tick. Failure callbacks for
        FAILED/CANCELLED/REJECTED entries still run (once, via process_outbound)."""
        for m in list(self.pending_outbound):
            if m.state == LXMessage.DELIVERED:
                self._dequeue(m)
            elif m.state in (LXMessage.FAILED, LXMessage.CANCELLED, LXMessage.REJECTED):
                self._process_one(m)

    def fail_message(self, msg):
        log("LXMF message to " + msg.destination_hash.hex()[:8] + " failed to send", LOG_NOTICE)
        msg.progress = 0.0
        self._dequeue(msg)
        if msg.state != LXMessage.REJECTED:
            msg.state = LXMessage.FAILED
        self._exec_failed(msg)
        self._release_link(msg.destination_hash)

    def _exec_failed(self, msg):
        for cb in (msg._failed_callback, self._failed_cb):
            if cb is not None:
                try:
                    cb(msg)
                except Exception as e:
                    log("LXMF failed callback error: " + str(e), LOG_ERROR)

    def cancel_outbound(self, message_id):
        """Cancel a queued message by its message_id/hash. True if found."""
        for msg in list(self.pending_outbound):
            if msg.message_id == message_id:
                msg.state = LXMessage.CANCELLED
                res = msg.resource_representation
                if msg.representation == LXMessage.RESOURCE and res is not None:
                    try:
                        res.cancel()
                    except Exception as e:
                        log("LXMF cancel error: " + str(e), LOG_DEBUG)
                self.process_outbound()
                return True
        return False

    def get_outbound_progress(self, message_id):
        for msg in self.pending_outbound:
            if msg.message_id == message_id:
                return msg.progress
        return None

    def _ensure_job(self):
        if self._job_task is not None:
            return
        coro = self._job_loop()
        try:
            import uasyncio as asyncio
            self._job_task = asyncio.create_task(coro)
        except Exception:
            coro.close()      # no loop (host tests): the caller drives process_outbound()

    async def _job_loop(self):
        import uasyncio as asyncio
        while True:
            try:
                self.process_outbound()
                self.clean_links()
            except Exception as e:
                log("LXMF router job error: " + str(e), LOG_ERROR)
            await asyncio.sleep(LXMRouter.PROCESSING_INTERVAL)

    def _later(self, delay, fn):
        """Run fn() after `delay` seconds. PORT: a task instead of a thread;
        with no running loop (host tests) it runs at once."""
        async def run():
            import uasyncio as asyncio
            await asyncio.sleep_ms(int(delay * 1000))
            try:
                fn()
            except Exception as e:
                log("LXMF deferred call error: " + str(e), LOG_ERROR)
        coro = run()
        try:
            import uasyncio as asyncio
            asyncio.create_task(coro)
        except Exception:
            coro.close()
            fn()

    def process_outbound(self):
        # PORT: no thread lock; a re-entrant call (a proof delivered inside
        # send(), a link established inside OutgoingLink()) is remembered and
        # run once the current pass is done instead of being lost.
        if self._processing:
            self._again = True
            return
        self._processing = True
        try:
            while True:
                self._again = False
                for msg in list(self.pending_outbound):
                    try:
                        self._process_one(msg)
                    except Exception as e:
                        log("LXMF outbound processing error: " + str(e), LOG_ERROR)
                        try:
                            self.fail_message(msg)
                        except Exception:
                            pass
                if not self._again:
                    break
        finally:
            self._processing = False

    def _process_one(self, msg):
        if msg not in self.pending_outbound:
            return          # already reaped (delivered promptly, or a re-entrant pass)
        st = msg.state
        if st == LXMessage.DELIVERED:
            log("Delivery has occurred for LXMF message to " + msg.destination_hash.hex()[:8]
                + ", removing from outbound queue", LOG_DEBUG)
            self._dequeue(msg)
            Transport.mark_in_use(msg.destination_hash)
            self._release_link(msg.destination_hash)
            return
        if st == LXMessage.CANCELLED or st == LXMessage.FAILED:
            self._dequeue(msg)
            self._exec_failed(msg)
            self._release_link(msg.destination_hash)
            return
        if st == LXMessage.REJECTED:
            log("Receiver rejected LXMF message to " + msg.destination_hash.hex()[:8], LOG_NOTICE)
            self._dequeue(msg)
            self._exec_failed(msg)
            self._release_link(msg.destination_hash)
            return
        if msg._pow_pending:
            return          # proof-of-work stamp still being searched

        h = msg.destination_hash
        if msg.progress is None or msg.progress < 0.01:
            msg.progress = 0.01

        if msg._destination is None:
            # PORT: peer identity not known yet. Each due attempt is a (debounced)
            # path request -- the answer carries the identity -- counted like
            # the reference's pathless path requests.
            if not self._resolve(msg):
                if msg.delivery_attempts <= self.attempt_limit(msg):
                    if self.attempt_due(msg, h):
                        msg.awaiting_path = False
                        msg.delivery_attempts += 1
                        self.request_path(h)
                        self.schedule_attempt(msg, h, path_request=True)
                elif self.attempt_due(msg, h):
                    log("Max delivery attempts reached, peer identity never learned: " + h.hex()[:8], LOG_DEBUG)
                    self.fail_message(msg)
                return
            self._prepare(msg)
            if msg._pow_pending:
                return

        if msg.method == LXMessage.OPPORTUNISTIC:
            self._process_opportunistic(msg, h)
        elif msg.method == LXMessage.DIRECT:
            self._process_direct(msg, h)
        else:
            log("Unsupported delivery method: " + str(msg.method), LOG_ERROR)
            self.fail_message(msg)

    def _process_opportunistic(self, msg, h):
        held = self.slow_path_held(msg, h)
        if not held and msg.delivery_attempts <= self.attempt_limit(msg):
            if not self.attempt_due(msg, h):
                return
            path_known = Transport.has_path(h)
            msg.awaiting_path = False
            if msg.delivery_attempts >= LXMRouter.MAX_PATHLESS_TRIES and not path_known:
                log("Requesting path to " + h.hex()[:8] + " after " + str(msg.delivery_attempts) + " pathless tries", LOG_DEBUG)
                msg.delivery_attempts += 1
                msg.sent_on_path = False
                self.request_path(h)
                self.schedule_attempt(msg, h, path_request=True)
                msg.progress = 0.01
            elif msg.delivery_attempts == LXMRouter.MAX_PATHLESS_TRIES + 1 and path_known and msg.sent_on_path:
                # Only rediscover if the previous attempt was actually sent from same path
                msg.delivery_attempts += 1
                msg.sent_on_path = False
                msg.awaiting_path = True
                if self.path_request_pending(h):
                    log("Opportunistic delivery to " + h.hex()[:8] + " still unsuccessful after "
                        + str(msg.delivery_attempts) + " attempts, path already being rediscovered", LOG_DEBUG)
                else:
                    log("Opportunistic delivery to " + h.hex()[:8] + " still unsuccessful after "
                        + str(msg.delivery_attempts) + " attempts, rediscovering path", LOG_DEBUG)
                    self.path_request_times[h] = time.time()
                    Transport.expire_path(h)
                    self._later(0.5, lambda: Transport.request_path(h))
                msg.next_delivery_attempt = time.time() + self.path_request_wait()
                msg.progress = 0.01
            else:
                prev_on_path = msg.sent_on_path
                msg.delivery_attempts += 1
                msg.sent_on_path = path_known
                msg.sent_on_slow_path = self.slow_path(h)
                self.schedule_attempt(msg, h)
                log("Opportunistic delivery attempt " + str(msg.delivery_attempts) + " to " + h.hex()[:8], LOG_DEBUG)
                try:
                    sent = msg.send()
                except Exception as e:
                    log("Error while sending LXMF message: " + str(e), LOG_ERROR)
                    self.fail_message(msg)
                    return
                if sent is False:
                    # PORT (the reference counts the attempt and calls it SENT even when
                    # Packet.send() transmitted nothing): a send that never left the node
                    # is not an attempt. Undo the bookkeeping so it neither burns one of the
                    # attempts nor counts as "sent on this path" (which would trigger route
                    # rediscovery), and try again at the next tick -- i.e. as soon as an
                    # interface is back. Bounded: MAX_UNSENT_TICKS consecutive failures fail it.
                    msg.delivery_attempts -= 1
                    msg.sent_on_path = prev_on_path
                    msg.sent_on_slow_path = False
                    msg.next_delivery_attempt = None
                    msg.receipt_timeout = None
                    msg.unsent_ticks += 1
                    if msg.unsent_ticks >= LXMRouter.MAX_UNSENT_TICKS:
                        log("LXMF message to " + h.hex()[:8] + " could not be transmitted for "
                            + str(msg.unsent_ticks) + " ticks, giving up", LOG_NOTICE)
                        self.fail_message(msg)
                else:
                    msg.unsent_ticks = 0
        elif not held and self.attempt_due(msg, h):
            log("Max delivery attempts reached for opportunistic LXMF message to " + h.hex()[:8], LOG_DEBUG)
            self.fail_message(msg)

    def _new_link(self, msg, h):
        from .link import OutgoingLink
        # sign_proofs: a peer that sends an LXMF packet back over this link wants it
        # proved, and validates the proof against the Ed25519 key we put in the link
        # request (reference Link.prove_packet signs with the link's own key; the
        # peer's Link.validate uses the request's peer_sig_pub). Without a real key
        # prove_packet() is a no-op and the peer resends. A real key costs one
        # Ed25519 key derivation per link: milliseconds with the native module, but
        # ~1.4-2 s of blocking pure Python without it, so a board without native
        # Ed25519 opens the link as before (random sig-pub): messages that arrive on
        # it are still delivered, just not proved.
        link = OutgoingLink(msg._destination, established_callback=self._link_established,
                            closed_callback=self._link_closed, sign_proofs=ed25519.have_native())
        link.resource_concluded_callback = self._link_resource_concluded
        # Safety net while the link is open: a reference peer may send an LXMF
        # message back over it (see _own_link_packet). Cleared by link._close().
        link.packet_callback = lambda plaintext, packet: self._own_link_packet(link, plaintext, packet)
        self.direct_links[h] = link
        return link

    def _process_direct(self, msg, h):
        held = self.slow_path_held(msg, h)
        if not held and msg.delivery_attempts <= self.attempt_limit(msg):
            link = self.direct_links.get(h)
            if link is not None:
                if link.status == 0x01:              # ACTIVE
                    if msg.progress is None or msg.progress < 0.05:
                        msg.progress = 0.05
                    if msg.state == LXMessage.OUTBOUND:
                        log("Starting transfer of LXMF message to " + h.hex()[:8] + " on link " + link.link_id.hex()[:8], LOG_DEBUG)
                        msg.sent_on_slow_path = self.slow_path(h)
                        msg._delivery_link = link
                        try:
                            msg.send()
                        except Exception as e:
                            log("Error while sending LXMF message: " + str(e), LOG_ERROR)
                            self.fail_message(msg)
                    elif msg.representation == LXMessage.RESOURCE:
                        res = msg.resource_representation
                        if res is not None:
                            try:
                                msg.progress = 0.10 + res.get_progress() * 0.90
                            except Exception:
                                pass
                elif link.status == 0x02:            # CLOSED
                    if getattr(link, "close_reason", None) == "stamp":
                        # The link-request stamp search failed or was refused as
                        # too expensive: another attempt would repeat it.
                        log("LXMF DIRECT link stamp failed, not retrying", LOG_ERROR)
                        self.direct_links.pop(h, None)
                        self.fail_message(msg)
                        return
                    if link.activated_at is not None:
                        log("The link to " + h.hex()[:8] + " was closed unexpectedly, retrying path request...", LOG_DEBUG)
                        self.request_path(h)
                    elif not msg.path_request_retried:
                        log("The link to " + h.hex()[:8] + " was never activated, retrying path request...", LOG_DEBUG)
                        self.request_path(h)
                        msg.path_request_retried = True
                    else:
                        log("The link to " + h.hex()[:8] + " was never activated", LOG_DEBUG)
                    msg._delivery_link = None
                    self.direct_links.pop(h, None)
                    if msg.delivery_attempts + 1 < self.attempt_limit(msg):
                        self.schedule_attempt(msg, h)
                # else: pending -- wait for it to become active or close
            elif self.attempt_due(msg, h):
                msg.delivery_attempts += 1
                msg.awaiting_path = False
                self.schedule_attempt(msg, h)
                if msg.delivery_attempts < self.attempt_limit(msg):
                    if Transport.has_path(h):
                        log("Establishing link to " + h.hex()[:8] + " for delivery attempt " + str(msg.delivery_attempts), LOG_DEBUG)
                        self._new_link(msg, h)
                        msg.progress = 0.03
                    else:
                        log("No path known for delivery attempt " + str(msg.delivery_attempts) + " to " + h.hex()[:8]
                            + ". Requesting path...", LOG_DEBUG)
                        self.request_path(h)
                        self.schedule_attempt(msg, h, path_request=True)
                        msg.progress = 0.01
                else:
                    log("Max delivery attempts reached for direct LXMF message to " + h.hex()[:8], LOG_DEBUG)
                    self.fail_message(msg)
        elif not held and self.attempt_due(msg, h):
            log("Max delivery attempts reached for direct LXMF message to " + h.hex()[:8], LOG_DEBUG)
            self.fail_message(msg)

    def _find_active_link_for(self, destination_hash):
        """An ACTIVE OutgoingLink we opened to this LXMF peer, or None. Only
        initiator links qualify, which is exactly what direct_links holds.
        PORT: reference 1.3.1 also reuses backchannel links (links the PEER
        opened to us, once it has identified itself); we do not, because
        reference LXMF before 1.3 wires its delivery callbacks only on links it
        ACCEPTS at its delivery destination, not on links it OPENED: a message
        sent back over the peer's link transfers and proves at the Resource
        layer but is silently discarded by the peer's app (seen with MeshChat
        image replies). A peer's version is not known here."""
        link = self.direct_links.get(destination_hash)
        if link is not None and link.status == 0x01:
            return link
        return None

    def _link_established(self, link):
        """A DIRECT link we opened is up: identify, then run the queue (the
        reference's link established callback is process_outbound)."""
        # Backchannel identification (reference LXMF does this on delivery
        # links it opens). MeshChatX's inbound policy reads the link's remote
        # identity to tell contacts from strangers BEFORE accepting a Resource
        # -- an anonymous link is a stranger even when we're in its contact
        # list, and the transfer is cancelled before the message ever arrives.
        # PORT: done at establishment, before the payload (the reference does
        # it after the first delivery).
        if self.delivery_identity is not None and not getattr(link, "lxm_identified", False):
            try:
                link.identify(self.delivery_identity)
                link.lxm_identified = True
            except Exception as e:
                log("LXMF link identify error: " + str(e), LOG_DEBUG)
        self.process_outbound()

    def _link_closed(self, link):
        # Failure of the link-request stamp search is final for the message:
        # let processing report it now rather than at the next tick.
        if getattr(link, "close_reason", None) == "stamp":
            self.process_outbound()

    def _link_resource_concluded(self, resource):
        msg = getattr(resource, "lxm", None)
        if msg is not None and getattr(resource, "is_initiator", False):
            msg._resource_concluded(resource)
        elif not getattr(resource, "is_initiator", False):
            # A Resource the PEER sent back over a link we opened (the resource
            # layer already proved it): hand it to the delivery path.
            self._handle_resource_concluded(resource)

    def _own_link_packet(self, link, plaintext, packet):
        """An LXMF packet the peer sent over a link WE opened: deliver it like
        one received on the delivery destination, and prove it (OutgoingLink
        does not prove CTX_NONE packets by itself)."""
        self._link_packet_received(plaintext, packet)
        try:
            link.prove_packet(packet)
        except Exception as e:
            log("LXMF own-link prove error: " + str(e), LOG_DEBUG)

    def _release_link(self, h):
        """Close the DIRECT link we opened to `h` unless a queued message for
        it still needs it (messages to one peer share a link). PORT, deviation
        from the reference (which holds delivery links open for 10 minutes):
        nothing here handles what a peer sends back on a link we opened beyond
        the safety net in _own_link_packet, and an idle link only costs
        keepalives, so the link is closed as soon as delivery is done."""
        link = self.direct_links.get(h)
        if link is None:
            return
        for m in self.pending_outbound:
            if (m.destination_hash == h and m.method == LXMessage.DIRECT
                    and m.state not in (LXMessage.DELIVERED, LXMessage.FAILED,
                                        LXMessage.CANCELLED, LXMessage.REJECTED)):
                return
        del self.direct_links[h]
        if link.status != 0x02:
            log("Closing LXMF link to " + h.hex()[:8] + " (delivery done)", LOG_VERBOSE)
            try:
                link.teardown()
            except Exception as e:
                log("LXMF link close error: " + str(e), LOG_DEBUG)

    def _dequeue(self, msg):
        msg._release_receipts()
        if msg in self.pending_outbound:
            self.pending_outbound.remove(msg)
            return True
        return False

    def clean_links(self):
        """Safety net: forget dead DIRECT links and close any open one that no
        queued message needs (normally _release_link already did)."""
        for h in list(self.direct_links):
            link = self.direct_links[h]
            if link.status == 0x02:
                if not any(m.destination_hash == h for m in self.pending_outbound):
                    del self.direct_links[h]
            else:
                self._release_link(h)

    # --- Tickets (LXMF stamps without proof-of-work) ---------------------

    _TICKET_FILE = "/lxmf_tickets"
    _MAX_TICKETS = 32

    def remember_ticket(self, message):
        """Capture a ticket the peer attached to a signature-validated
        message (reference LXMRouter.lxmf_delivery): FIELD_TICKET =
        [expires_unix, 16-byte ticket]. Replies to that peer then carry
        truncated_hash(ticket + message_id) as their stamp."""
        if not getattr(message, "signature_validated", False):
            return
        fields = getattr(message, "fields", None)
        if not isinstance(fields, dict) or FIELD_TICKET not in fields:
            return
        entry = fields[FIELD_TICKET]
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            return
        expires, ticket = entry[0], entry[1]
        if not isinstance(expires, (int, float)) or expires <= _unix_now():
            return
        if not isinstance(ticket, bytes) or len(ticket) != LXMessage.TICKET_LENGTH:
            return
        current = self.outbound_tickets.get(message.source_hash)
        if current is not None and current[1] == ticket and current[0] == expires:
            return          # same ticket re-sent: nothing to write to flash
        if (message.source_hash not in self.outbound_tickets
                and len(self.outbound_tickets) >= self._MAX_TICKETS):
            # Evict the soonest-expiring entry.
            victim = min(self.outbound_tickets, key=lambda k: self.outbound_tickets[k][0])
            del self.outbound_tickets[victim]
        self.outbound_tickets[message.source_hash] = [expires, ticket]
        log("LXMF ticket from " + message.source_hash.hex()[:8] + " remembered", LOG_DEBUG)
        self._save_tickets()

    def get_outbound_ticket(self, destination_hash):
        """The unexpired ticket to stamp a message to destination_hash with,
        or None."""
        entry = self.outbound_tickets.get(destination_hash)
        if entry is None:
            return None
        if entry[0] <= _unix_now():
            del self.outbound_tickets[destination_hash]
            return None
        return entry[1]

    def _load_tickets(self):
        if not self.storagepath:
            return
        try:
            with open(self.storagepath + self._TICKET_FILE, "rb") as f:
                data = umsgpack.unpackb(f.read())
            if isinstance(data, dict):
                now = _unix_now()
                for k, v in data.items():
                    if (isinstance(k, bytes) and isinstance(v, (list, tuple))
                            and len(v) >= 2 and v[0] > now):
                        self.outbound_tickets[k] = [v[0], v[1]]
        except OSError:
            pass          # no file yet
        except Exception as e:
            log("LXMF ticket load error: " + str(e), LOG_DEBUG)

    def _save_tickets(self):
        if not self.storagepath:
            return
        try:
            with open(self.storagepath + self._TICKET_FILE, "wb") as f:
                f.write(umsgpack.packb(self.outbound_tickets))
        except Exception as e:
            log("LXMF ticket save error: " + str(e), LOG_DEBUG)

    def _on_link_established(self, link):
        """Called when a link is established to our delivery destination."""
        link.set_packet_callback(self._link_packet_received)
        link.resource_concluded_callback = self._handle_resource_concluded
        if self._progress_callback:
            link.resource_started_callback = self._on_resource_started
        log("LXMF link established: " + link.link_id.hex()[:8], LOG_DEBUG)

    def _on_resource_started(self, resource):
        """Wire the router's progress callback into a just-accepted resource."""
        resource.progress_callback = self._progress_callback

    def _link_packet_received(self, plaintext, packet):
        """Handle single-packet LXMF message received on a link (CTX_NONE)."""
        try:
            # DIRECT delivery sends full packed: dest_hash + source_hash + sig + payload
            # (unlike OPPORTUNISTIC which strips dest_hash)
            lxmf_data = plaintext
            log("LXMF link packet: " + str(len(lxmf_data)) + "B", LOG_DEBUG)

            message = LXMessage.unpack_from_bytes(lxmf_data)
            message.transport_encrypted = True
            message.transport_encryption = "Curve25519"
            self.remember_ticket(message)

            if message.signature_validated:
                log("LXMF link message from " + message.source_hash.hex()[:8] +
                    ": " + (message.content_as_string() or "(binary)"), LOG_NOTICE)
            else:
                reason = "unknown source" if message.unverified_reason == LXMessage.SOURCE_UNKNOWN else "invalid signature"
                log("LXMF unverified link message (" + reason + ") from " +
                    message.source_hash.hex()[:8], LOG_NOTICE)

            # Dedup check
            if message.hash in self.delivered_ids:
                log("LXMF duplicate ignored: " + message.hash.hex()[:8], LOG_DEBUG)
                return

            self.delivered_ids[message.hash] = time.time()
            self._clean_delivered_ids()

            if self._delivery_callback:
                try:
                    self._delivery_callback(message)
                except Exception as e:
                    log("LXMF delivery callback error: " + str(e), LOG_ERROR)

        except Exception as e:
            log("LXMF link packet error: " + str(e), LOG_ERROR)

    def _handle_resource_concluded(self, resource):
        """Called when a Resource transfer completes on a link."""
        from .resource import COMPLETE
        if resource.status != COMPLETE:
            log("LXMF resource not complete, ignoring", LOG_DEBUG)
            return

        try:
            data = resource.data
            if data is None or len(data) < 2 * LXMessage.DESTINATION_LENGTH + LXMessage.SIGNATURE_LENGTH:
                log("LXMF resource data too short", LOG_DEBUG)
                return

            # Check if this is a response (request_id set) — not LXMF delivery
            if resource.request_id is not None:
                log("LXMF resource is a response, not a delivery", LOG_DEBUG)
                return

            # DIRECT delivery sends full packed: dest_hash + source_hash + sig + payload
            lxmf_data = data
            log("LXMF resource unpacking: " + str(len(lxmf_data)) + "B", LOG_DEBUG)

            message = LXMessage.unpack_from_bytes(lxmf_data)
            message.transport_encrypted = True
            message.transport_encryption = "Curve25519"
            self.remember_ticket(message)

            if message.signature_validated:
                log("LXMF resource message from " + message.source_hash.hex()[:8] +
                    ": " + (message.content_as_string() or "(binary)"), LOG_NOTICE)
            else:
                reason = "unknown source" if message.unverified_reason == LXMessage.SOURCE_UNKNOWN else "invalid signature"
                log("LXMF unverified resource message (" + reason + ") from " +
                    message.source_hash.hex()[:8], LOG_NOTICE)

            # Remember which LXMF peer this link carries (see _link_packet_received).
            link = getattr(resource, "link", None)
            if link is not None:
                link.lxmf_source_hash = message.source_hash

            # Dedup check
            if message.hash in self.delivered_ids:
                log("LXMF duplicate ignored: " + message.hash.hex()[:8], LOG_DEBUG)
                return

            self.delivered_ids[message.hash] = time.time()
            self._clean_delivered_ids()

            if self._delivery_callback:
                try:
                    self._delivery_callback(message)
                except Exception as e:
                    log("LXMF delivery callback error: " + str(e), LOG_ERROR)

        except Exception as e:
            log("LXMF resource delivery error: " + str(e), LOG_ERROR)

    def _delivery_packet(self, data, packet):
        """Handle incoming opportunistic LXMF packet"""
        try:
            log("LXMF delivery_packet: " + str(len(data)) + "B", LOG_DEBUG)

            # For opportunistic delivery, prepend destination hash
            # (it's inferred from the packet destination, not included in payload)
            dest_hash = self.delivery_destination.hash
            lxmf_data = dest_hash + data

            log("LXMF unpacking: " + str(len(lxmf_data)) + "B total", LOG_DEBUG)
            message = LXMessage.unpack_from_bytes(lxmf_data)
            self.remember_ticket(message)

            if message.signature_validated:
                log("LXMF message from " + message.source_hash.hex()[:8] +
                    ": " + (message.content_as_string() or "(binary)"), LOG_NOTICE)
            else:
                reason = "unknown source" if message.unverified_reason == LXMessage.SOURCE_UNKNOWN else "invalid signature"
                log("LXMF unverified message (" + reason + ") from " +
                    message.source_hash.hex()[:8], LOG_NOTICE)

            # Dedup check (the proof is sent by Transport for every packet:
            # proof_strategy PROVE_ALL, see register_delivery_identity)
            if message.hash in self.delivered_ids:
                log("LXMF duplicate ignored: " + message.hash.hex()[:8], LOG_DEBUG)
                return

            self.delivered_ids[message.hash] = time.time()
            self._clean_delivered_ids()

            # Deliver to application
            if self._delivery_callback:
                try:
                    self._delivery_callback(message)
                except Exception as e:
                    log("LXMF delivery callback error: " + str(e), LOG_ERROR)

        except Exception as e:
            log("LXMF delivery packet error: " + str(e), LOG_ERROR)

    def _announce_handler(self, destination_hash, app_data, packet):
        """Called by Transport when any announce is received"""
        self.handle_announce(destination_hash, app_data)

    @staticmethod
    def _parse_stamp_cost(app_data):
        """Inbound stamp cost a peer announces: msgpack [name, cost, ...]."""
        try:
            if app_data and ((0x90 <= app_data[0] <= 0x9f) or app_data[0] == 0xdc):
                d = umsgpack.unpackb(app_data)
                if isinstance(d, list) and len(d) >= 2:
                    # LXMF 1.3.x transitional layout: [name, ceil(cost), sf, cost]
                    # (exact fractional cost in element 3); later and earlier
                    # versions carry the cost in element 1.
                    for c in ((d[3],) if len(d) > 3 else ()) + (d[1],):
                        if isinstance(c, (int, float)) and not isinstance(c, bool) and 0 < c <= 255:
                            return c
        except Exception:
            pass
        return None

    def handle_announce(self, destination_hash, app_data):
        """Process an LXMF delivery announce from a peer.
        Call this from your announce handler."""
        display_name = None
        if app_data:
            display_name = self._parse_display_name(app_data)

        self.peers[destination_hash] = {
            "name": display_name,
            "timestamp": time.time(),
            "stamp_cost": self._parse_stamp_cost(app_data),
        }

        log("LXMF peer: " + (display_name or "?") +
            " [" + destination_hash.hex()[:8] + "]", LOG_VERBOSE)

        if self._announce_callback:
            try:
                self._announce_callback(destination_hash, display_name)
            except Exception as e:
                log("Announce callback error: " + str(e), LOG_ERROR)

    @staticmethod
    def _parse_display_name(app_data):
        """Parse display name from LXMF announce app_data"""
        try:
            if not app_data or len(app_data) == 0:
                return None

            # Version 0.5.0+ format: msgpack [name_bytes, stamp_cost]
            first = app_data[0]
            if (0x90 <= first <= 0x9f) or first == 0xdc:
                peer_data = umsgpack.unpackb(app_data)
                if isinstance(peer_data, list) and len(peer_data) >= 1:
                    dn = peer_data[0]
                    if dn is None or dn is False:
                        return None
                    if isinstance(dn, bytes):
                        return dn.decode("utf-8")
                    return str(dn)

            # Legacy format: raw string
            return app_data.decode("utf-8")

        except Exception:
            return None

    def _clean_delivered_ids(self):
        """Remove expired message IDs from dedup cache"""
        now = time.time()
        expired = [h for h, ts in self.delivered_ids.items()
                   if now - ts > self.MESSAGE_EXPIRY]
        for h in expired:
            del self.delivered_ids[h]

    @staticmethod
    def display_name_from_app_data(app_data):
        """Public helper to parse display name from app_data"""
        return LXMRouter._parse_display_name(app_data)
