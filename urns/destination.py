# µReticulum Destination
# Endpoint addressing and announce management

import os
import time
from . import const
from .log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_EXTREME, LOG_NOTICE
from .identity import Identity
from .crypto import Token


class Destination:
    # Type constants
    SINGLE = const.DEST_SINGLE
    GROUP  = const.DEST_GROUP
    PLAIN  = const.DEST_PLAIN
    LINK   = const.DEST_LINK

    # Direction constants
    IN  = const.DIR_IN
    OUT = const.DIR_OUT

    # Proof strategies
    PROVE_NONE = const.PROVE_NONE
    PROVE_APP  = const.PROVE_APP
    PROVE_ALL  = const.PROVE_ALL

    # Request policies
    ALLOW_NONE = 0x00
    ALLOW_ALL  = 0x01
    ALLOW_LIST = 0x02

    PR_TAG_WINDOW = 30
    RATCHET_COUNT = const.RATCHET_COUNT
    RATCHET_INTERVAL = const.RATCHET_INTERVAL

    @staticmethod
    def expand_name(identity, app_name, *aspects):
        if "." in app_name:
            raise ValueError("Dots not allowed in app names")
        name = app_name
        for aspect in aspects:
            if "." in aspect:
                raise ValueError("Dots not allowed in aspects")
            name += "." + aspect
        if identity is not None:
            name += "." + identity.hexhash
        return name

    @staticmethod
    def hash(identity, app_name, *aspects):
        name_hash = Identity.full_hash(
            Destination.expand_name(None, app_name, *aspects).encode("utf-8")
        )[:(Identity.NAME_HASH_LENGTH // 8)]
        addr_hash_material = name_hash
        if identity is not None:
            if isinstance(identity, Identity):
                addr_hash_material += identity.hash
            elif isinstance(identity, bytes) and len(identity) == const.TRUNCATED_HASHLENGTH // 8:
                addr_hash_material += identity
            else:
                raise TypeError("Invalid identity material for hash")
        return Identity.full_hash(addr_hash_material)[:const.TRUNCATED_HASHLENGTH // 8]

    def __init__(self, identity, direction, type, app_name, *aspects):
        if "." in app_name:
            raise ValueError("Dots not allowed in app names")
        if type not in (Destination.SINGLE, Destination.GROUP, Destination.PLAIN, Destination.LINK):
            raise ValueError("Unknown destination type")
        if direction not in (Destination.IN, Destination.OUT):
            raise ValueError("Unknown destination direction")

        self.accept_link_requests = True
        self.type = type
        self.direction = direction
        self.proof_strategy = Destination.PROVE_NONE
        self.ratchets = None
        self.ratchet_interval = Destination.RATCHET_INTERVAL
        self.retained_ratchets = Destination.RATCHET_COUNT
        self.latest_ratchet_time = None
        self.latest_ratchet_id = None
        self._enforce_ratchets = False
        self.mtu = 0
        self.links = []
        self.path_responses = {}

        # Callbacks
        self.link_established_callback = None
        self.packet_callback = None
        self.proof_requested_callback = None
        self.request_handlers = {}
        self._announce_handler = None

        if identity is None and direction == Destination.IN and type != Destination.PLAIN:
            identity = Identity()
            aspects = aspects + (identity.hexhash,)

        if identity is None and direction == Destination.OUT and type != Destination.PLAIN:
            raise ValueError("Outbound SINGLE destination requires an identity")

        if identity is not None and type == Destination.PLAIN:
            raise TypeError("PLAIN destinations cannot hold an identity")

        self.identity = identity
        self.name = Destination.expand_name(identity, app_name, *aspects)
        self.hash = Destination.hash(self.identity, app_name, *aspects)
        self.name_hash = Identity.full_hash(
            Destination.expand_name(None, app_name, *aspects).encode("utf-8")
        )[:(Identity.NAME_HASH_LENGTH // 8)]
        self.hexhash = self.hash.hex()
        self.default_app_data = None

        from .transport import Transport
        Transport.register_destination(self)

    def __str__(self):
        return "<" + self.name + ":" + self.hexhash + ">"

    def announce(self, app_data=None, path_response=False, attached_interface=None, tag=None, send=True):
        if self.type != Destination.SINGLE:
            raise TypeError("Only SINGLE destinations can be announced")
        if self.direction != Destination.IN:
            raise TypeError("Only IN destinations can be announced")

        ratchet = b""
        destination_hash = self.hash
        random_hash = Identity.get_random_hash()[0:5] + int(time.time()).to_bytes(5, "big")

        if self.ratchets is not None:
            self.rotate_ratchets()
            ratchet = Identity._ratchet_public_bytes(self.ratchets[0])
            Identity._remember_ratchet(self.hash, ratchet)

        if app_data is None and self.default_app_data is not None:
            if isinstance(self.default_app_data, bytes):
                app_data = self.default_app_data
            elif callable(self.default_app_data):
                returned = self.default_app_data()
                if isinstance(returned, bytes):
                    app_data = returned

        signed_data = self.hash + self.identity.get_public_key() + self.name_hash + random_hash + ratchet
        if app_data is not None:
            signed_data += app_data

        signature = self.identity.sign(signed_data)
        announce_data = self.identity.get_public_key() + self.name_hash + random_hash + ratchet + signature

        if app_data is not None:
            announce_data += app_data

        if ratchet:
            context_flag = const.FLAG_SET
        else:
            context_flag = const.FLAG_UNSET

        announce_context = const.CTX_PATH_RESPONSE if path_response else const.CTX_NONE

        from .packet import Packet
        announce_packet = Packet(
            self, announce_data, const.PKT_ANNOUNCE,
            context=announce_context,
            attached_interface=attached_interface,
            context_flag=context_flag,
        )

        if send:
            announce_packet.send()
        else:
            return announce_packet

    def rotate_ratchets(self):
        if self.ratchets is not None:
            now = time.time()
            if self.latest_ratchet_time is None or now > self.latest_ratchet_time + self.ratchet_interval:
                new_ratchet = Identity._generate_ratchet()
                self.ratchets.insert(0, new_ratchet)
                self.latest_ratchet_time = now
                if len(self.ratchets) > self.retained_ratchets:
                    self.ratchets = self.ratchets[:self.retained_ratchets]

    def enable_ratchets(self):
        self.ratchets = []
        self.latest_ratchet_time = 0

    def encrypt(self, plaintext):
        if self.type == Destination.PLAIN:
            return plaintext
        if self.type == Destination.SINGLE and self.identity is not None:
            selected_ratchet = Identity.get_ratchet(self.hash)
            if selected_ratchet:
                self.latest_ratchet_id = Identity._get_ratchet_id(selected_ratchet)
            return self.identity.encrypt(plaintext, ratchet=selected_ratchet)
        if self.type == Destination.GROUP:
            if hasattr(self, 'prv') and self.prv is not None:
                return self.prv.encrypt(plaintext)
            else:
                raise ValueError("No key for GROUP destination")

    def decrypt(self, ciphertext):
        if self.type == Destination.PLAIN:
            return ciphertext
        if self.type == Destination.SINGLE and self.identity is not None:
            if self.ratchets:
                try:
                    return self.identity.decrypt(
                        ciphertext, ratchets=self.ratchets,
                        enforce_ratchets=self._enforce_ratchets,
                        ratchet_id_receiver=self,
                    )
                except:
                    return None
            else:
                return self.identity.decrypt(
                    ciphertext, ratchets=None,
                    enforce_ratchets=self._enforce_ratchets,
                    ratchet_id_receiver=self,
                )
        if self.type == Destination.GROUP:
            if hasattr(self, 'prv') and self.prv is not None:
                return self.prv.decrypt(ciphertext)
            else:
                raise ValueError("No key for GROUP destination")

    def sign(self, message):
        if self.type == Destination.SINGLE and self.identity is not None:
            return self.identity.sign(message)
        return None

    def receive(self, packet):
        if packet.packet_type == const.PKT_LINKREQUEST:
            if self.accept_link_requests:
                # Link handling will be implemented in Phase 4
                pass
        else:
            plaintext = self.decrypt(packet.data)
            if plaintext is not None:
                packet.destination = self
                packet.ratchet_id = self.latest_ratchet_id
                if packet.packet_type == const.PKT_DATA:
                    if self.packet_callback is not None:
                        try:
                            self.packet_callback(plaintext, packet)
                        except Exception as e:
                            log("Packet callback error: " + str(e), LOG_ERROR)
                return True
            return False

    def set_link_established_callback(self, callback):
        self.link_established_callback = callback

    def set_packet_callback(self, callback):
        self.packet_callback = callback

    def set_proof_requested_callback(self, callback):
        self.proof_requested_callback = callback

    def set_proof_strategy(self, proof_strategy):
        self.proof_strategy = proof_strategy

    def set_default_app_data(self, app_data=None):
        self.default_app_data = app_data

    def create_keys(self):
        if self.type == Destination.GROUP:
            self.prv_bytes = Token.generate_key()
            self.prv = Token(self.prv_bytes)

    def accepts_links(self, accepts=None):
        if accepts is None:
            return self.accept_link_requests
        self.accept_link_requests = bool(accepts)
