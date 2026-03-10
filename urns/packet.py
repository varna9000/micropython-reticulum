# µReticulum Packet
# Packet framing and handling

import struct
import time
from . import const
from .log import log, LOG_DEBUG, LOG_ERROR, LOG_EXTREME


class Packet:
    # Types (re-exported for convenience)
    DATA        = const.PKT_DATA
    ANNOUNCE    = const.PKT_ANNOUNCE
    LINKREQUEST = const.PKT_LINKREQUEST
    PROOF       = const.PKT_PROOF

    # Header types
    HEADER_1 = const.HDR_1
    HEADER_2 = const.HDR_2

    # Contexts
    NONE          = const.CTX_NONE
    RESOURCE      = const.CTX_RESOURCE
    RESOURCE_ADV  = const.CTX_RESOURCE_ADV
    RESOURCE_REQ  = const.CTX_RESOURCE_REQ
    RESOURCE_PRF  = const.CTX_RESOURCE_PRF
    CACHE_REQUEST = const.CTX_CACHE_REQUEST
    REQUEST       = const.CTX_REQUEST
    RESPONSE      = const.CTX_RESPONSE
    PATH_RESPONSE = const.CTX_PATH_RESPONSE
    CHANNEL       = const.CTX_CHANNEL
    KEEPALIVE     = const.CTX_KEEPALIVE
    LINKIDENTIFY  = const.CTX_LINKIDENTIFY
    LINKCLOSE     = const.CTX_LINKCLOSE
    LINKPROOF     = const.CTX_LINKPROOF
    LRRTT         = const.CTX_LRRTT
    LRPROOF       = const.CTX_LRPROOF

    # Flags
    FLAG_SET   = const.FLAG_SET
    FLAG_UNSET = const.FLAG_UNSET

    # Size constants
    HEADER_MAXSIZE = const.HEADER_MAXSIZE
    MDU           = const.MDU
    ENCRYPTED_MDU = const.ENCRYPTED_MDU
    PLAIN_MDU     = const.PLAIN_MDU

    TIMEOUT_PER_HOP = const.DEFAULT_PER_HOP_TIMEOUT

    def __init__(self, destination, data, packet_type=None, context=None,
                 transport_type=None, header_type=None, transport_id=None,
                 attached_interface=None, create_receipt=True, context_flag=None):

        if packet_type is None:
            packet_type = const.PKT_DATA
        if context is None:
            context = const.CTX_NONE
        if transport_type is None:
            transport_type = const.TRANSPORT_BROADCAST
        if header_type is None:
            header_type = const.HDR_1
        if context_flag is None:
            context_flag = const.FLAG_UNSET

        if destination is not None:
            self.header_type = header_type
            self.packet_type = packet_type
            self.transport_type = transport_type
            self.context = context
            self.context_flag = context_flag
            self.hops = 0
            self.destination = destination
            self.transport_id = transport_id
            self.data = data
            self.flags = self._get_packed_flags()
            self.raw = None
            self.packed = False
            self.sent = False
            self.create_receipt = create_receipt
            self.receipt = None
            self.fromPacked = False
        else:
            # Reconstruct from raw bytes
            self.raw = data
            self.packed = True
            self.fromPacked = True
            self.create_receipt = False
            self.destination = None
            self.data = None
            self.transport_id = None

        self.MTU = const.MTU
        self.sent_at = None
        self.packet_hash = None
        self.ratchet_id = None
        self.attached_interface = attached_interface
        self.receiving_interface = None
        self.rssi = None
        self.snr = None
        self.q = None
        self.destination_hash = None
        self.destination_type = None
        self.link = None
        self.ciphertext = None
        self.plaintext = None

    def _get_packed_flags(self):
        if self.context == const.CTX_LRPROOF:
            dest_type = const.DEST_LINK
        else:
            dest_type = self.destination.type
        return (self.header_type << 6) | (self.context_flag << 5) | (self.transport_type << 4) | (dest_type << 2) | self.packet_type

    def pack(self):
        self.destination_hash = self.destination.hash
        self.header = b""
        self.header += struct.pack("!B", self.flags)
        self.header += struct.pack("!B", self.hops)

        if self.context == const.CTX_LRPROOF:
            self.header += self.destination.link_id
            self.ciphertext = self.data
        else:
            if self.header_type == const.HDR_1:
                self.header += self.destination.hash

                if self.packet_type in (const.PKT_ANNOUNCE, const.PKT_LINKREQUEST):
                    self.ciphertext = self.data
                elif self.packet_type == const.PKT_PROOF and self.context == const.CTX_RESOURCE_PRF:
                    self.ciphertext = self.data
                elif self.packet_type == const.PKT_PROOF and self.destination.type == const.DEST_LINK:
                    self.ciphertext = self.data
                elif self.context in (const.CTX_RESOURCE, const.CTX_KEEPALIVE, const.CTX_CACHE_REQUEST):
                    self.ciphertext = self.data
                else:
                    self.ciphertext = self.destination.encrypt(self.data)
                    if hasattr(self.destination, 'latest_ratchet_id'):
                        self.ratchet_id = self.destination.latest_ratchet_id

            elif self.header_type == const.HDR_2:
                if self.transport_id is not None:
                    self.header += self.transport_id
                    self.header += self.destination.hash
                    if self.packet_type == const.PKT_ANNOUNCE:
                        self.ciphertext = self.data
                else:
                    raise OSError("Header type 2 requires transport ID")

        self.header += bytes([self.context])
        self.raw = self.header + self.ciphertext

        if len(self.raw) > self.MTU:
            raise OSError("Packet size " + str(len(self.raw)) + " exceeds MTU " + str(self.MTU))

        self.packed = True
        self.update_hash()

    def unpack(self):
        try:
            self.flags = self.raw[0]
            self.hops = self.raw[1]

            self.header_type = (self.flags & 0b01000000) >> 6
            self.context_flag = (self.flags & 0b00100000) >> 5
            self.transport_type = (self.flags & 0b00010000) >> 4
            self.destination_type = (self.flags & 0b00001100) >> 2
            self.packet_type = (self.flags & 0b00000011)

            DST_LEN = const.TRUNCATED_HASHLENGTH // 8

            if self.header_type == const.HDR_2:
                self.transport_id = self.raw[2:DST_LEN + 2]
                self.destination_hash = self.raw[DST_LEN + 2:2 * DST_LEN + 2]
                self.context = self.raw[2 * DST_LEN + 2]
                self.data = self.raw[2 * DST_LEN + 3:]
            else:
                self.transport_id = None
                self.destination_hash = self.raw[2:DST_LEN + 2]
                self.context = self.raw[DST_LEN + 2]
                self.data = self.raw[DST_LEN + 3:]

            self.packed = False
            self.update_hash()
            return True

        except Exception as e:
            log("Malformed packet: " + str(e), LOG_EXTREME)
            return False

    def send(self):
        if not self.sent:
            if not self.packed:
                self.pack()
            from .transport import Transport
            if Transport.outbound(self):
                return self.receipt
            else:
                self.sent = False
                self.receipt = None
                return False
        else:
            raise OSError("Packet already sent")

    def resend(self):
        if self.sent:
            self.pack()
            from .transport import Transport
            if Transport.outbound(self):
                return self.receipt
            else:
                self.sent = False
                self.receipt = None
                return False
        else:
            raise OSError("Packet not yet sent")

    def update_hash(self):
        self.packet_hash = self.get_hash()

    def get_hash(self):
        from .identity import Identity
        return Identity.full_hash(self.get_hashable_part())

    def getTruncatedHash(self):
        from .identity import Identity
        return Identity.truncated_hash(self.get_hashable_part())

    def get_hashable_part(self):
        hashable_part = bytes([self.raw[0] & 0b00001111])
        if self.header_type == const.HDR_2:
            hashable_part += self.raw[(const.TRUNCATED_HASHLENGTH // 8) + 2:]
        else:
            hashable_part += self.raw[2:]
        return hashable_part

    def prove(self, destination=None):
        """Send a proof (delivery receipt) back to the sender"""
        if self.destination and self.destination.identity and self.destination.identity.prv:
            signature = self.destination.identity.sign(self.packet_hash)
            try:
                import gc; gc.collect()
            except:
                pass
            # Implicit proof: just the signature
            proof_data = signature

            if destination is None:
                destination = self.generate_proof_destination()

            proof = Packet(destination, proof_data, const.PKT_PROOF,
                           attached_interface=self.receiving_interface)
            try:
                import gc; gc.collect()
            except:
                pass
            proof.send()
            log("Proof sent for " + self.packet_hash.hex()[:8], LOG_DEBUG)
        else:
            log("Cannot prove packet: no signing identity", LOG_DEBUG)

    def generate_proof_destination(self):
        return ProofDestination(self)


class ProofDestination:
    def __init__(self, packet):
        self.hash = packet.get_hash()[:const.TRUNCATED_HASHLENGTH // 8]
        self.type = const.DEST_SINGLE

    def encrypt(self, plaintext):
        return plaintext


class LinkDestination:
    """Pseudo-destination for packets addressed to a link_id."""
    def __init__(self, link_id):
        self.hash = link_id
        self.link_id = link_id
        self.type = const.DEST_LINK

    def encrypt(self, plaintext):
        return plaintext


class PacketReceipt:
    FAILED    = 0x00
    SENT      = 0x01
    DELIVERED = 0x02
    CULLED    = 0xFF

    def __init__(self, packet):
        self.hash = packet.get_hash()
        self.truncated_hash = packet.getTruncatedHash()
        self.sent = True
        self.sent_at = time.time()
        self.proved = False
        self.status = PacketReceipt.SENT
        self.destination = packet.destination
        self.delivery_callback = None
        self.timeout_callback = None
        self.concluded_at = None
        self.proof_packet = None
        self.timeout = const.DEFAULT_PER_HOP_TIMEOUT

    def get_status(self):
        return self.status

    def validate_proof_packet(self, proof_packet):
        return self.validate_proof(proof_packet.data, proof_packet)

    def validate_proof(self, proof, proof_packet=None):
        from .identity import Identity
        EXPL_LENGTH = Identity.HASHLENGTH // 8 + Identity.SIGLENGTH // 8
        IMPL_LENGTH = Identity.SIGLENGTH // 8

        if len(proof) == EXPL_LENGTH:
            proof_hash = proof[:Identity.HASHLENGTH // 8]
            signature = proof[Identity.HASHLENGTH // 8:]
            if proof_hash == self.hash and hasattr(self.destination, 'identity') and self.destination.identity:
                if self.destination.identity.validate(signature, self.hash):
                    self.status = PacketReceipt.DELIVERED
                    self.proved = True
                    self.concluded_at = time.time()
                    self.proof_packet = proof_packet
                    if self.delivery_callback:
                        try:
                            self.delivery_callback(self)
                        except Exception as e:
                            log("Delivery callback error: " + str(e), LOG_ERROR)
                    return True
            return False
        elif len(proof) == IMPL_LENGTH:
            if not hasattr(self.destination, 'identity') or not self.destination.identity:
                return False
            signature = proof[:Identity.SIGLENGTH // 8]
            if self.destination.identity.validate(signature, self.hash):
                self.status = PacketReceipt.DELIVERED
                self.proved = True
                self.concluded_at = time.time()
                self.proof_packet = proof_packet
                if self.delivery_callback:
                    try:
                        self.delivery_callback(self)
                    except Exception as e:
                        log("Delivery callback error: " + str(e), LOG_ERROR)
                return True
            return False
        return False

    def get_rtt(self):
        if self.concluded_at:
            return self.concluded_at - self.sent_at
        return None

    def is_timed_out(self):
        return self.sent_at + self.timeout < time.time()

    def check_timeout(self):
        if self.status == PacketReceipt.SENT and self.is_timed_out():
            if self.timeout == -1:
                self.status = PacketReceipt.CULLED
            else:
                self.status = PacketReceipt.FAILED
            self.concluded_at = time.time()
            if self.timeout_callback:
                try:
                    self.timeout_callback(self)
                except:
                    pass

    def set_timeout(self, timeout):
        self.timeout = float(timeout)

    def set_delivery_callback(self, callback):
        self.delivery_callback = callback

    def set_timeout_callback(self, callback):
        self.timeout_callback = callback
