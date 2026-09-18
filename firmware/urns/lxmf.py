# µReticulum LXMF - Lightweight Extensible Message Format
# Wire-compatible with reference LXMF for MeshChat/Sideband interop
# Supports opportunistic (single-packet) and direct (link) message delivery

import time, sys
from . import umsgpack
from .identity import Identity
from .destination import Destination
from .packet import Packet
from .transport import Transport
from .log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE, LOG_INFO
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

    # Sizes
    DESTINATION_LENGTH = Identity.TRUNCATED_HASHLENGTH // 8   # 16
    SIGNATURE_LENGTH   = Identity.SIGLENGTH // 8              # 64
    TIMESTAMP_SIZE     = 8
    TICKET_LENGTH      = 16   # reference LXMessage.TICKET_LENGTH (truncated hash size)
    STRUCT_OVERHEAD    = 8
    LXMF_OVERHEAD      = 2 * DESTINATION_LENGTH + SIGNATURE_LENGTH + TIMESTAMP_SIZE + STRUCT_OVERHEAD  # 112

    # Max content that fits in a single encrypted packet (opportunistic)
    ENCRYPTED_PACKET_MAX_CONTENT = Packet.ENCRYPTED_MDU + TIMESTAMP_SIZE - LXMF_OVERHEAD + DESTINATION_LENGTH

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
        # Stamp (5th payload element). We only ever generate TICKET stamps:
        # truncated_hash(ticket + message_id), free to compute, accepted by
        # reference LXMF peers that hand us a ticket. PoW stamps are out of
        # reach on an MCU, so outbound_ticket is the only way to satisfy a
        # peer that requires stamps.
        self.stamp = None
        self.outbound_ticket = None
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
        self._delivery_callback = callback

    def register_failed_callback(self, callback):
        self._failed_callback = callback

    def pack(self):
        """Pack message into wire format"""
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

        # Ticket stamp, appended as the 5th payload element AFTER hashing and
        # signing (reference LXMessage.pack: hash/signature cover the
        # 4-element payload; the receiver strips element 4 before verifying).
        if self.outbound_ticket:
            self.stamp = Identity.truncated_hash(self.outbound_ticket + self.message_id)
            payload.append(self.stamp)

        # Assemble packed message
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

    def send(self):
        """Send the message via the chosen method"""
        if not self.packed:
            self.pack()

        if self.method == LXMessage.OPPORTUNISTIC:
            # For opportunistic, we send to the destination directly
            # The packet data excludes the destination hash (inferred from packet header)
            data = self.packed[self.DESTINATION_LENGTH:]
            pkt = Packet(self._destination, data)
            pkt.send()
            self.state = LXMessage.SENT
            self.transport_encrypted = True
            self.transport_encryption = "Curve25519"
            log("Sent opportunistic LXMF message to " + self.destination_hash.hex()[:8], LOG_NOTICE)

            if self._delivery_callback:
                try:
                    self._delivery_callback(self)
                except Exception as e:
                    log("Delivery callback error: " + str(e), LOG_ERROR)

        elif self.method == LXMessage.DIRECT:
            # DIRECT delivery handled by LXMRouter._send_direct()
            pass
        else:
            log("Unsupported delivery method: " + str(self.method), LOG_ERROR)
            self.state = LXMessage.FAILED

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

        # Tickets peers gave us (FIELD_TICKET in their messages) so we can
        # reply with a free ticket stamp: dest_hash -> [expires_unix, ticket].
        # Persisted: a reference peer re-issues a ticket at most once a day
        # (TICKET_INTERVAL), so losing it on reboot would fail replies for
        # up to a day.
        self.outbound_tickets = {}
        self._load_tickets()

        self.peers = {}  # hash -> {name, timestamp, identity}
        self.delivered_ids = {}  # message_hash -> timestamp (dedup)

        self.MESSAGE_EXPIRY = 30 * 24 * 60 * 60  # 30 days

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
            self.delivery_destination.announce(app_data=app_data)
            log("LXMF announced as: " + (self.display_name or "unnamed"), LOG_NOTICE)

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

    def send_message(self, destination_hash, content, title="",
                     fields=None, desired_method=None):
        """Send an LXMF message to a destination hash.

        Resilient delivery chain:
          1. reuse a link we previously opened to the peer (DIRECT), else
          2. send opportunistically if a path is known (auto-upgrades to
             DIRECT via a fresh OutgoingLink when the payload is too big), else
          3. request a path and defer the send until the route is learned.

        Returns the LXMessage when sent/started, True when queued pending a path,
        or None on hard failure (reachable but identity still unknown).
        """
        from .transport import Transport

        have_link = self._find_active_link_for(destination_hash) is not None

        # No route yet: solicit a path and defer the send. The retry re-enters
        # this method once the path (and the peer's identity, carried in the
        # path-response announce) is known.
        if not have_link and not Transport.has_path(destination_hash):
            log("No path to " + destination_hash.hex()[:8] + ", requesting path", LOG_VERBOSE)
            Transport.ensure_path(
                destination_hash,
                on_found=lambda: self.send_message(
                    destination_hash, content, title, fields, desired_method),
                on_timeout=lambda: log(
                    "LXMF send to " + destination_hash.hex()[:8]
                    + " failed: no path", LOG_ERROR),
            )
            return True   # queued

        # Reachable — we need the peer's keys to encrypt.
        dest_identity = Identity.recall(destination_hash)
        if dest_identity is None:
            log("Cannot send LXMF: unknown identity for " + destination_hash.hex()[:8], LOG_ERROR)
            return None

        dest = Destination(dest_identity, Destination.OUT,
                           Destination.SINGLE, APP_NAME, "delivery")

        source = Destination(self.delivery_identity, Destination.OUT,
                             Destination.SINGLE, APP_NAME, "delivery")

        # Prefer an already-open link when the caller hasn't forced a method:
        # avoids a redundant path request when we can answer over the link.
        if have_link and desired_method is None:
            desired_method = LXMessage.DIRECT

        msg = LXMessage(
            destination=dest,
            source=source,
            content=content,
            title=title,
            fields=fields,
            desired_method=desired_method or LXMessage.OPPORTUNISTIC,
        )

        # A peer that requires stamps gave us a ticket; stamp with it so the
        # reply isn't dropped as "invalid stamp".
        msg.outbound_ticket = self.get_outbound_ticket(destination_hash)

        msg.pack()

        if msg.method == LXMessage.OPPORTUNISTIC:
            msg.send()
        elif msg.method == LXMessage.DIRECT:
            self._send_direct(msg, dest)

        return msg

    def _find_active_link_for(self, destination_hash):
        """Return an ACTIVE OutgoingLink we previously opened to this LXMF
        peer, or None. Only initiator links qualify: reference LXMF wires
        delivery callbacks on links it ACCEPTS at its delivery destination,
        not on links it OPENED — a message sent back over the peer's own
        link transfers and proves at the Resource layer but is silently
        discarded by the peer's app (observed with MeshChat image replies)."""
        from .transport import Transport
        from .link import OutgoingLink
        for link in Transport.active_links:
            if link.status != 0x01:  # ACTIVE
                continue
            if isinstance(link, OutgoingLink) and link.destination.hash == destination_hash:
                return link
        return None

    def _send_direct(self, message, destination):
        """Send LXMF message via DIRECT link delivery (link + Resource).

        Reuses an OutgoingLink we already hold to the peer, else opens a
        fresh one (path availability is guaranteed by the has_path/
        ensure_path gate in send_message). Never sends over a peer-initiated
        link — see _find_active_link_for.
        """
        from .link import OutgoingLink
        from . import const

        def deliver_on(link, owns_link):
            if owns_link:
                # Backchannel identification (reference LXMF does this on
                # every delivery link it opens). MeshChatX's inbound policy
                # reads the link's remote identity to tell contacts from
                # strangers BEFORE accepting a Resource -- an anonymous link
                # is a stranger even when we're in its contact list, and the
                # transfer is cancelled before the message ever arrives.
                # Must go out before the payload/advertisement.
                if self.delivery_identity is not None:
                    try:
                        link.identify(self.delivery_identity)
                    except Exception as e:
                        log("LXMF link identify error: " + str(e), LOG_DEBUG)
            packed = message.packed
            # Single link packet capacity: ~415B after Token encryption
            if len(packed) <= 415:
                link.send(packed, const.CTX_NONE)
                message.state = LXMessage.SENT
                log("LXMF DIRECT sent as packet: " + str(len(packed)) + "B", LOG_VERBOSE)
                if owns_link:
                    link.teardown()
            else:
                from .resource import Resource
                # Chain with any existing resource-concluded callback (e.g. the
                # router's incoming-delivery handler on a peer-initiated link)
                # so future incoming resources still fire their handler.
                previous_cb = link.resource_concluded_callback
                def on_resource_done(r):
                    if getattr(r, "is_initiator", False):
                        self._direct_resource_concluded(r, message, link, owns_link)
                    elif previous_cb is not None:
                        previous_cb(r)
                link.resource_concluded_callback = on_resource_done
                Resource(link, packed, is_response=False)
                log("LXMF DIRECT sending as resource: " + str(len(packed)) + "B", LOG_VERBOSE)

        existing = self._find_active_link_for(message.destination_hash)
        if existing is not None:
            log("LXMF DIRECT reusing active link " + existing.link_id.hex()[:8] +
                " to " + message.destination_hash.hex()[:8], LOG_NOTICE)
            deliver_on(existing, owns_link=False)
            return

        def on_established(link):
            deliver_on(link, owns_link=True)

        def on_closed(link):
            if message.state < LXMessage.SENT:
                if self._retry_direct(message, destination):
                    return
                message.state = LXMessage.FAILED
                log("LXMF DIRECT link closed before delivery", LOG_ERROR)

        OutgoingLink(destination, established_callback=on_established, closed_callback=on_closed)
        log("LXMF DIRECT delivery initiated to " + message.destination_hash.hex()[:8], LOG_NOTICE)

    def _retry_direct(self, message, destination):
        """Bounded re-attempt of a DIRECT delivery whose link died before the
        message got through, or whose resource transfer failed. On a congested
        half-duplex hop the establishment packets (LR, proof, LRRTT) can be
        lost to the relay's own transmissions; reference LXMF retries failed
        outbounds, and without this a single lost packet permanently fails the
        delivery. Counts attempts on the message: 3 total."""
        attempts = getattr(message, "direct_attempts", 0) + 1
        if attempts >= 3:
            return False
        message.direct_attempts = attempts
        log("LXMF DIRECT retry " + str(attempts + 1) + "/3 to "
            + message.destination_hash.hex()[:8], LOG_NOTICE)
        try:
            message.state = LXMessage.SENDING
            self._send_direct(message, destination)
            return True
        except Exception as e:
            log("LXMF DIRECT retry error: " + str(e), LOG_ERROR)
            return False

    def _direct_resource_concluded(self, resource, message, link, owns_link=True):
        """Called when outgoing DIRECT Resource transfer completes."""
        from .resource import COMPLETE
        if resource.status == COMPLETE:
            message.state = LXMessage.DELIVERED
            log("LXMF DIRECT delivered: " + message.destination_hash.hex()[:8], LOG_NOTICE)
        else:
            # Retry on a FRESH link: an unanswered advertisement usually means
            # the peer's side of this link never became established. Mark the
            # message FAILED before teardown so the link's closed-callback
            # doesn't also schedule a retry, and grab the destination before
            # _close() nulls it; _retry_direct resets the state on re-send.
            destination = link.destination
            message.state = LXMessage.FAILED
            if owns_link:
                link.teardown()
            if destination is not None and self._retry_direct(message, destination):
                return
            log("LXMF DIRECT resource failed", LOG_ERROR)
            return
        # Only teardown links we opened ourselves — if we reused a peer-
        # initiated link, tearing it down would close their channel.
        if owns_link:
            link.teardown()

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

            # Dedup check
            if message.hash in self.delivered_ids:
                log("LXMF duplicate ignored: " + message.hash.hex()[:8], LOG_DEBUG)
                return

            self.delivered_ids[message.hash] = time.time()
            self._clean_delivered_ids()

            # Send delivery proof (so sender knows we received it)
            try:
                import gc; gc.collect()
            except:
                pass
            packet.prove()

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

    def handle_announce(self, destination_hash, app_data):
        """Process an LXMF delivery announce from a peer.
        Call this from your announce handler."""
        display_name = None
        if app_data:
            display_name = self._parse_display_name(app_data)

        self.peers[destination_hash] = {
            "name": display_name,
            "timestamp": time.time(),
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
