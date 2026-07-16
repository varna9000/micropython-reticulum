# µReticulum Channel
# Reliable, ordered, windowed message stream over a Link — the substrate rnsh
# (and RNS.Buffer) ride on. Port of reference RNS/Channel.py, adapted for
# MicroPython + urns:
#   * cooperative event loop -> no threading locks (all ops run on the loop)
#   * plain lists for the tx/rx rings (MicroPython collections.deque has no
#     insert()/remove())
#   * ACK = link packet PROOF via urns PacketReceipt; RESEND re-encrypts a fresh
#     packet (urns Packet.resend() is a no-op, and identical ciphertext would be
#     dropped by the receiver's dedup) — same sequence, new IV, new packet hash.
#
# A Channel is obtained from a link with link.get_channel(); messages are
# MessageBase subclasses registered with register_message_type().

import struct
import time
from . import const
from .log import log, LOG_ERROR, LOG_DEBUG, LOG_EXTREME

# Message delivery states (mirror reference MessageState)
MSGSTATE_NEW       = 0
MSGSTATE_SENT      = 1
MSGSTATE_DELIVERED = 2
MSGSTATE_FAILED    = 3

# ChannelException type codes (subset we actually raise)
ME_NO_MSG_TYPE     = 0
ME_NOT_REGISTERED  = 2
ME_LINK_NOT_READY  = 3
ME_TOO_BIG         = 5


class SystemMessageTypes:
    """MSGTYPEs >= 0xf000 are reserved for internal stack use. Registering one
    needs is_system_type=True (see Channel._register_message_type)."""
    SMT_STREAM_DATA = 0xff00   # RNS.Buffer stream chunks


class ChannelException(Exception):
    def __init__(self, ce_type, msg=""):
        super().__init__(msg)
        self.type = ce_type


class MessageBase:
    """Base for any message sent/received on a Channel. Subclasses set the
    class attribute MSGTYPE (unique) and implement pack()/unpack(). MSGTYPEs
    >= 0xf000 are reserved for the stack (e.g. SMT_STREAM_DATA) — register
    those with _register_message_type(is_system_type=True)."""
    MSGTYPE = None

    def pack(self):
        raise NotImplementedError()

    def unpack(self, raw):
        raise NotImplementedError()


class Envelope:
    """Wraps a message for transport + tracks its channel state."""

    def __init__(self, outlet, message=None, raw=None, sequence=None):
        self.ts = time.time()
        self.message = message
        self.raw = raw
        self.packet = None
        self.sequence = sequence
        self.outlet = outlet
        self.tries = 0
        self.unpacked = False
        self.packed = False
        self.tracked = False

    def pack(self):
        if self.message.__class__.MSGTYPE is None:
            raise ChannelException(ME_NO_MSG_TYPE, "message lacks MSGTYPE")
        data = self.message.pack()
        # 6-byte big-endian header: MSGTYPE, sequence, length
        self.raw = struct.pack(">HHH", self.message.MSGTYPE, self.sequence, len(data)) + data
        self.packed = True
        return self.raw

    def unpack(self, message_factories):
        msgtype, self.sequence, length = struct.unpack(">HHH", self.raw[:6])
        raw = self.raw[6:]
        ctor = message_factories.get(msgtype, None)
        if ctor is None:
            raise ChannelException(ME_NOT_REGISTERED,
                                   "no constructor for Channel MSGTYPE " + hex(msgtype))
        message = ctor()
        message.unpack(raw)
        self.unpacked = True
        self.message = message
        return message


class Channel:
    # Windowing (reference parity). On slow links (rtt > RTT_SLOW, i.e. LoRa)
    # everything is pinned to 1 — strictly one packet outstanding.
    WINDOW                  = 2
    WINDOW_MIN              = 2
    WINDOW_MIN_LIMIT_MEDIUM = 5
    WINDOW_MIN_LIMIT_FAST   = 16
    WINDOW_MAX_SLOW         = 5
    WINDOW_MAX_MEDIUM       = 12
    WINDOW_MAX_FAST         = 48
    WINDOW_MAX              = WINDOW_MAX_FAST
    WINDOW_FLEXIBILITY      = 4
    FAST_RATE_THRESHOLD     = 10
    RTT_FAST                = 0.18
    RTT_MEDIUM              = 0.75
    RTT_SLOW                = 1.45

    SEQ_MAX     = 0xFFFF
    SEQ_MODULUS = 0x10000

    def __init__(self, outlet):
        self._outlet = outlet
        self._tx_ring = []          # list of Envelope (ordered by sequence)
        self._rx_ring = []          # list of Envelope (ordered by sequence)
        self._message_callbacks = []
        self._next_sequence = 0
        self._next_rx_sequence = 0
        self._message_factories = {}
        self._max_tries = 5
        self.fast_rate_rounds = 0
        self.medium_rate_rounds = 0

        if self._outlet.rtt > Channel.RTT_SLOW:
            self.window = 1
            self.window_max = 1
            self.window_min = 1
            self.window_flexibility = 1
        else:
            self.window = Channel.WINDOW
            self.window_max = Channel.WINDOW_MAX_SLOW
            self.window_min = Channel.WINDOW_MIN
            self.window_flexibility = Channel.WINDOW_FLEXIBILITY

    # --- registration / handlers -------------------------------------------

    def register_message_type(self, message_class):
        """Register a user message class (MSGTYPE < 0xf000)."""
        self._register_message_type(message_class, is_system_type=False)

    def _register_message_type(self, message_class, is_system_type=False):
        """Register a message class. System types (MSGTYPE >= 0xf000, e.g.
        SMT_STREAM_DATA) require is_system_type=True (used by Buffer)."""
        if not (isinstance(message_class, type) and issubclass(message_class, MessageBase)):
            raise ChannelException(ME_NO_MSG_TYPE, "not a MessageBase subclass")
        if message_class.MSGTYPE is None:
            raise ChannelException(ME_NO_MSG_TYPE, "message class has no MSGTYPE")
        if message_class.MSGTYPE >= 0xf000 and not is_system_type:
            raise ChannelException(ME_NO_MSG_TYPE, "MSGTYPE >= 0xf000 is system-reserved")
        self._message_factories[message_class.MSGTYPE] = message_class

    def add_message_handler(self, callback):
        if callback not in self._message_callbacks:
            self._message_callbacks.append(callback)

    def remove_message_handler(self, callback):
        if callback in self._message_callbacks:
            self._message_callbacks.remove(callback)

    def shutdown(self):
        self._message_callbacks = []
        self._clear_rings()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
        return False

    def _clear_rings(self):
        for envelope in self._tx_ring:
            if envelope.packet is not None:
                self._outlet.set_packet_timeout_callback(envelope.packet, None)
                self._outlet.set_packet_delivered_callback(envelope.packet, None)
            envelope.tracked = False
        self._tx_ring = []
        self._rx_ring = []

    # --- ring management ----------------------------------------------------

    def _emplace_envelope(self, envelope, ring):
        i = 0
        for existing in ring:
            if envelope.sequence == existing.sequence:
                return False   # duplicate
            if (envelope.sequence < existing.sequence
                    and not (self._next_rx_sequence - envelope.sequence) > (Channel.SEQ_MAX // 2)):
                ring.insert(i, envelope)
                envelope.tracked = True
                return True
            i += 1
        envelope.tracked = True
        ring.append(envelope)
        return True

    def _run_callbacks(self, message):
        for cb in list(self._message_callbacks):
            try:
                if cb(message):
                    return
            except Exception as e:
                log("Channel callback error: " + str(e), LOG_ERROR)

    # --- receive ------------------------------------------------------------

    def _receive(self, raw):
        try:
            envelope = Envelope(self._outlet, raw=raw)
            message = envelope.unpack(self._message_factories)

            if envelope.sequence < self._next_rx_sequence:
                window_overflow = (self._next_rx_sequence + Channel.WINDOW_MAX) % Channel.SEQ_MODULUS
                if window_overflow < self._next_rx_sequence:
                    if envelope.sequence > window_overflow:
                        log("Channel: invalid seq " + str(envelope.sequence), LOG_EXTREME)
                        return
                else:
                    log("Channel: invalid seq " + str(envelope.sequence), LOG_EXTREME)
                    return

            is_new = self._emplace_envelope(envelope, self._rx_ring)
            if not is_new:
                log("Channel: duplicate message", LOG_EXTREME)
                return

            # Deliver contiguously from _next_rx_sequence, buffering gaps.
            contiguous = []
            for e in self._rx_ring:
                if e.sequence == self._next_rx_sequence:
                    contiguous.append(e)
                    self._next_rx_sequence = (self._next_rx_sequence + 1) % Channel.SEQ_MODULUS
                    if self._next_rx_sequence == 0:
                        for e2 in self._rx_ring:
                            if e2.sequence == self._next_rx_sequence:
                                contiguous.append(e2)
                                self._next_rx_sequence = (self._next_rx_sequence + 1) % Channel.SEQ_MODULUS

            for e in contiguous:
                m = e.message if e.unpacked else e.unpack(self._message_factories)
                if e in self._rx_ring:
                    self._rx_ring.remove(e)
                self._run_callbacks(m)

        except Exception as e:
            log("Channel receive error: " + str(e), LOG_ERROR)

    # --- send ---------------------------------------------------------------

    def is_ready_to_send(self):
        if not self._outlet.is_usable:
            return False
        outstanding = 0
        for envelope in self._tx_ring:
            if not envelope.packet or self._outlet.get_packet_state(envelope.packet) != MSGSTATE_DELIVERED:
                outstanding += 1
        return outstanding < self.window

    def send(self, message):
        if not self.is_ready_to_send():
            raise ChannelException(ME_LINK_NOT_READY, "channel not ready")

        reserved_sequence = self._next_sequence
        envelope = Envelope(self._outlet, message=message, sequence=reserved_sequence)
        envelope.pack()
        if len(envelope.raw) > self._outlet.mdu:
            raise ChannelException(ME_TOO_BIG,
                                   "message too big: " + str(len(envelope.raw)) + " > " + str(self._outlet.mdu))
        self._next_sequence = (reserved_sequence + 1) % Channel.SEQ_MODULUS

        envelope.packet = self._outlet.send(envelope.raw)
        if envelope.packet is None or getattr(envelope.packet, "receipt", None) is None:
            self._next_sequence = reserved_sequence
            raise ChannelException(ME_LINK_NOT_READY, "outlet did not transmit")

        self._emplace_envelope(envelope, self._tx_ring)
        envelope.tries += 1
        self._outlet.set_packet_delivered_callback(envelope.packet, self._packet_delivered)
        self._outlet.set_packet_timeout_callback(envelope.packet, self._packet_timeout,
                                                 self._get_packet_timeout_time(envelope.tries))
        self._update_packet_timeouts()
        if self._outlet.get_packet_state(envelope.packet) == MSGSTATE_DELIVERED:
            self._packet_delivered(envelope.packet)
        return envelope

    # --- ACK / retransmit ---------------------------------------------------

    def _find_envelope(self, packet):
        target_id = self._outlet.get_packet_id(packet)
        for e in self._tx_ring:
            if e.packet is not None and self._outlet.get_packet_id(e.packet) == target_id:
                return e
        return None

    def _packet_delivered(self, packet):
        envelope = self._find_envelope(packet)
        if envelope is None:
            return
        envelope.tracked = False
        if envelope in self._tx_ring:
            self._tx_ring.remove(envelope)
            if self.window < self.window_max:
                self.window += 1
            rtt = self._outlet.rtt
            if rtt != 0:
                if rtt > Channel.RTT_FAST:
                    self.fast_rate_rounds = 0
                    if rtt > Channel.RTT_MEDIUM:
                        self.medium_rate_rounds = 0
                    else:
                        self.medium_rate_rounds += 1
                        if self.window_max < Channel.WINDOW_MAX_MEDIUM and self.medium_rate_rounds == Channel.FAST_RATE_THRESHOLD:
                            self.window_max = Channel.WINDOW_MAX_MEDIUM
                            self.window_min = Channel.WINDOW_MIN_LIMIT_MEDIUM
                else:
                    self.fast_rate_rounds += 1
                    if self.window_max < Channel.WINDOW_MAX_FAST and self.fast_rate_rounds == Channel.FAST_RATE_THRESHOLD:
                        self.window_max = Channel.WINDOW_MAX_FAST
                        self.window_min = Channel.WINDOW_MIN_LIMIT_FAST

    def _get_packet_timeout_time(self, tries):
        return pow(1.5, tries - 1) * max(self._outlet.rtt * 2.5, 0.025) * (len(self._tx_ring) + 1.5)

    def _update_packet_timeouts(self):
        for envelope in self._tx_ring:
            updated = self._get_packet_timeout_time(envelope.tries)
            p = envelope.packet
            if p is not None and getattr(p, "receipt", None) is not None and p.receipt.timeout:
                if updated > p.receipt.timeout:
                    p.receipt.set_timeout(updated)

    def _packet_timeout(self, packet):
        if self._outlet.get_packet_state(packet) == MSGSTATE_DELIVERED:
            return
        envelope = self._find_envelope(packet)
        if envelope is None:
            return

        if envelope.tries >= self._max_tries:
            log("Channel: retry count exceeded, tearing down link", LOG_ERROR)
            self.shutdown()
            self._outlet.timed_out()
            return

        envelope.tries += 1
        if self.window > self.window_min:
            self.window -= 1
            if self.window_max > (self.window_min + self.window_flexibility):
                self.window_max -= 1

        # Re-encrypt into a fresh packet (new IV/hash, same sequence + envelope).
        new_packet = self._outlet.resend(envelope.packet)
        if new_packet is None or getattr(new_packet, "receipt", None) is None:
            return
        envelope.packet = new_packet
        self._outlet.set_packet_delivered_callback(new_packet, self._packet_delivered)
        self._outlet.set_packet_timeout_callback(new_packet, self._packet_timeout,
                                                 self._get_packet_timeout_time(envelope.tries))
        self._update_packet_timeouts()
        if self._outlet.get_packet_state(new_packet) == MSGSTATE_DELIVERED:
            self._packet_delivered(new_packet)

    @property
    def mdu(self):
        mdu = self._outlet.mdu - 6   # 6-byte envelope header
        return 0xFFFF if mdu > 0xFFFF else mdu


class _ChannelDestination:
    """Pseudo-destination for CHANNEL packets. Addresses the link_id like
    LinkDestination, but also carries the remote identity so the returning
    packet PROOF (the Channel ACK) validates in PacketReceipt.validate_proof."""

    def __init__(self, link):
        self.hash = link.link_id
        self.link_id = link.link_id
        self.type = const.DEST_LINK
        self.identity = link.destination.identity if link.destination is not None else None

    def encrypt(self, plaintext):
        return plaintext   # channel payload is already link-Token encrypted


class LinkChannelOutlet:
    """Adapts an urns (Outgoing)Link to the Channel outlet interface. Sends
    CHANNEL packets (link-Token encrypted, with a receipt) and re-encrypts on
    resend so retransmits carry a fresh IV/hash and pass the receiver's dedup."""

    def __init__(self, link):
        self.link = link

    def _make_and_send(self, raw):
        link = self.link
        if link is None or raw is None or getattr(link, "_token", None) is None:
            return None
        ciphertext = link._token.encrypt(raw)
        from .packet import Packet
        packet = Packet(_ChannelDestination(link), ciphertext, const.PKT_DATA,
                        context=const.CTX_CHANNEL, create_receipt=True)
        packet._chan_raw = raw   # keep plaintext envelope for re-encrypt-on-resend
        packet.send()
        return packet

    def send(self, raw):
        return self._make_and_send(raw)

    def resend(self, packet):
        return self._make_and_send(getattr(packet, "_chan_raw", None))

    @property
    def mdu(self):
        return getattr(self.link, "mdu", const.MDU)

    @property
    def rtt(self):
        return getattr(self.link, "rtt", 0) or 0

    @property
    def is_usable(self):
        return self.link is not None and getattr(self.link, "status", None) == 0x01  # ACTIVE

    def get_packet_state(self, packet):
        from .packet import PacketReceipt
        if packet is None or getattr(packet, "receipt", None) is None:
            return MSGSTATE_FAILED
        st = packet.receipt.get_status()
        if st == PacketReceipt.SENT:
            return MSGSTATE_SENT
        if st == PacketReceipt.DELIVERED:
            return MSGSTATE_DELIVERED
        return MSGSTATE_FAILED

    def timed_out(self):
        try:
            self.link.teardown()
        except Exception:
            pass

    def get_packet_id(self, packet):
        if packet is not None and getattr(packet, "raw", None) is not None:
            return packet.get_hash()
        return None

    def set_packet_timeout_callback(self, packet, callback, timeout=None):
        if packet is None or getattr(packet, "receipt", None) is None:
            return
        if timeout is not None:
            packet.receipt.set_timeout(timeout)
        if callback is None:
            packet.receipt.set_timeout_callback(None)
        else:
            def inner(receipt, _p=packet):
                callback(_p)
            packet.receipt.set_timeout_callback(inner)

    def set_packet_delivered_callback(self, packet, callback):
        if packet is None or getattr(packet, "receipt", None) is None:
            return
        if callback is None:
            packet.receipt.set_delivery_callback(None)
        else:
            def inner(receipt, _p=packet):
                callback(_p)
            packet.receipt.set_delivery_callback(inner)
