# µReticulum Identity
# Key management, encryption, signing

import os
import time
from . import const
from .log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_EXTREME, LOG_NOTICE
from .crypto import (
    X25519PrivateKey, X25519PublicKey,
    Ed25519PrivateKey, Ed25519PublicKey,
    Token, sha256, sha512, hkdf,
)


class Identity:
    CURVE = "Curve25519"
    KEYSIZE = const.KEYSIZE
    RATCHETSIZE = const.RATCHETSIZE
    RATCHET_EXPIRY = const.RATCHET_EXPIRY
    TOKEN_OVERHEAD = const.TOKEN_OVERHEAD
    AES128_BLOCKSIZE = const.AES128_BLOCKSIZE
    HASHLENGTH = const.HASHLENGTH
    SIGLENGTH = const.SIGLENGTH
    NAME_HASH_LENGTH = const.NAME_HASH_LENGTH
    TRUNCATED_HASHLENGTH = const.TRUNCATED_HASHLENGTH
    DERIVED_KEY_LENGTH = const.DERIVED_KEY_LENGTH

    known_destinations = {}
    known_ratchets = {}
    storagepath = "/rns"  # Set by Reticulum.__init__

    @staticmethod
    def remember(packet_hash, destination_hash, public_key, app_data=None):
        if len(public_key) != Identity.KEYSIZE // 8:
            raise TypeError("Invalid public key size: " + str(len(public_key)))
        Identity.known_destinations[destination_hash] = [time.time(), packet_hash, public_key, app_data]

    @staticmethod
    def recall(target_hash, from_identity_hash=False):
        if from_identity_hash:
            for dh in Identity.known_destinations:
                if target_hash == Identity.truncated_hash(Identity.known_destinations[dh][2]):
                    idata = Identity.known_destinations[dh]
                    identity = Identity(create_keys=False)
                    identity.load_public_key(idata[2])
                    identity.app_data = idata[3]
                    return identity
            return None
        else:
            if target_hash in Identity.known_destinations:
                idata = Identity.known_destinations[target_hash]
                identity = Identity(create_keys=False)
                identity.load_public_key(idata[2])
                identity.app_data = idata[3]
                return identity
            # Check registered destinations
            from . import transport
            for dest in transport.Transport.destinations:
                if target_hash == dest.hash:
                    identity = Identity(create_keys=False)
                    identity.load_public_key(dest.identity.get_public_key())
                    identity.app_data = None
                    return identity
            return None

    @staticmethod
    def recall_app_data(destination_hash):
        if destination_hash in Identity.known_destinations:
            return Identity.known_destinations[destination_hash][3]
        return None

    @staticmethod
    def full_hash(data):
        return sha256(data)

    @staticmethod
    def truncated_hash(data):
        return Identity.full_hash(data)[:(Identity.TRUNCATED_HASHLENGTH // 8)]

    @staticmethod
    def get_random_hash():
        return Identity.truncated_hash(os.urandom(Identity.TRUNCATED_HASHLENGTH // 8))

    @staticmethod
    def current_ratchet_id(destination_hash):
        ratchet = Identity.get_ratchet(destination_hash)
        if ratchet is None:
            return None
        return Identity._get_ratchet_id(ratchet)

    @staticmethod
    def _get_ratchet_id(ratchet_pub_bytes):
        return Identity.full_hash(ratchet_pub_bytes)[:Identity.NAME_HASH_LENGTH // 8]

    @staticmethod
    def _ratchet_public_bytes(ratchet):
        return X25519PrivateKey.from_private_bytes(ratchet).public_key().public_bytes()

    @staticmethod
    def _generate_ratchet():
        ratchet_prv = X25519PrivateKey.generate()
        return ratchet_prv.private_bytes()

    @staticmethod
    def _remember_ratchet(destination_hash, ratchet):
        try:
            if destination_hash in Identity.known_ratchets:
                if Identity.known_ratchets[destination_hash] == ratchet:
                    return
            Identity.known_ratchets[destination_hash] = ratchet
        except Exception as e:
            log("Could not remember ratchet: " + str(e), LOG_ERROR)

    @staticmethod
    def get_ratchet(destination_hash):
        if destination_hash in Identity.known_ratchets:
            return Identity.known_ratchets[destination_hash]
        return None

    @staticmethod
    def validate_announce(packet, only_validate_signature=False):
        try:
            keysize = Identity.KEYSIZE // 8
            ratchetsize = Identity.RATCHETSIZE // 8
            name_hash_len = Identity.NAME_HASH_LENGTH // 8
            sig_len = Identity.SIGLENGTH // 8
            destination_hash = packet.destination_hash

            public_key = packet.data[:keysize]

            log("Announce validate: data=" + str(len(packet.data)) + "B ctx_flag=" + str(packet.context_flag), LOG_DEBUG)

            if packet.context_flag == const.FLAG_SET:
                name_hash = packet.data[keysize:keysize + name_hash_len]
                random_hash = packet.data[keysize + name_hash_len:keysize + name_hash_len + 10]
                ratchet = packet.data[keysize + name_hash_len + 10:keysize + name_hash_len + 10 + ratchetsize]
                signature = packet.data[keysize + name_hash_len + 10 + ratchetsize:keysize + name_hash_len + 10 + ratchetsize + sig_len]
                app_data = b""
                if len(packet.data) > keysize + name_hash_len + 10 + sig_len + ratchetsize:
                    app_data = packet.data[keysize + name_hash_len + 10 + sig_len + ratchetsize:]
            else:
                ratchet = b""
                name_hash = packet.data[keysize:keysize + name_hash_len]
                random_hash = packet.data[keysize + name_hash_len:keysize + name_hash_len + 10]
                signature = packet.data[keysize + name_hash_len + 10:keysize + name_hash_len + 10 + sig_len]
                app_data = b""
                if len(packet.data) > keysize + name_hash_len + 10 + sig_len:
                    app_data = packet.data[keysize + name_hash_len + 10 + sig_len:]

            log("Announce fields: pubkey=" + str(len(public_key)) + " name_hash=" + str(len(name_hash)) + " random=" + str(len(random_hash)) + " ratchet=" + str(len(ratchet)) + " sig=" + str(len(signature)) + " app=" + str(len(app_data)), LOG_DEBUG)

            signed_data = destination_hash + public_key + name_hash + random_hash + ratchet + app_data
            log("Announce signed_data=" + str(len(signed_data)) + "B dest=" + destination_hash.hex()[:8] + " pubkey=" + public_key[:4].hex(), LOG_DEBUG)

            if not len(packet.data) > Identity.KEYSIZE // 8 + Identity.NAME_HASH_LENGTH // 8 + 10 + Identity.SIGLENGTH // 8:
                app_data = None

            announced_identity = Identity(create_keys=False)
            announced_identity.load_public_key(public_key)
            log("Announce identity loaded: hash=" + str(announced_identity.hexhash), LOG_DEBUG)

            sig_valid = announced_identity.validate(signature, signed_data)
            log("Announce sig_valid=" + str(sig_valid), LOG_DEBUG)

            if announced_identity.pub is not None and sig_valid:
                if only_validate_signature:
                    return True

                hash_material = name_hash + announced_identity.hash
                expected_hash = Identity.full_hash(hash_material)[:const.TRUNCATED_HASHLENGTH // 8]

                if destination_hash == expected_hash:
                    if destination_hash in Identity.known_destinations:
                        if public_key != Identity.known_destinations[destination_hash][2]:
                            log("Announce public key mismatch - possible hash collision", LOG_ERROR)
                            return False

                    Identity.remember(packet.get_hash(), destination_hash, public_key, app_data)

                    if ratchet:
                        Identity._remember_ratchet(destination_hash, ratchet)

                    return True
                else:
                    log("Invalid announce: destination mismatch", LOG_DEBUG)
                    return False
            else:
                log("Invalid announce: bad signature", LOG_DEBUG)
                return False

        except Exception as e:
            log("Error validating announce: " + str(e), LOG_ERROR)
            return False

    @staticmethod
    def save_known_destinations():
        """Persist known destinations to flash storage"""
        try:
            import json
            data = {}
            for dh in Identity.known_destinations:
                entry = Identity.known_destinations[dh]
                # Convert bytes keys and values to hex for JSON
                key_hex = dh.hex()
                data[key_hex] = [
                    entry[0],  # timestamp
                    entry[1].hex() if entry[1] else None,  # packet hash
                    entry[2].hex() if entry[2] else None,  # public key
                    entry[3].hex() if isinstance(entry[3], bytes) else None,  # app data
                ]
            with open(Identity.storagepath + "/known_destinations.json", "w") as f:
                json.dump(data, f)
            log("Saved " + str(len(data)) + " known destinations", LOG_DEBUG)
        except Exception as e:
            log("Error saving known destinations: " + str(e), LOG_ERROR)

    @staticmethod
    def load_known_destinations():
        """Load known destinations from flash storage"""
        try:
            import json
            with open(Identity.storagepath + "/known_destinations.json", "r") as f:
                data = json.load(f)
            for key_hex in data:
                entry = data[key_hex]
                dh = bytes.fromhex(key_hex)
                Identity.known_destinations[dh] = [
                    entry[0],
                    bytes.fromhex(entry[1]) if entry[1] else None,
                    bytes.fromhex(entry[2]) if entry[2] else None,
                    bytes.fromhex(entry[3]) if entry[3] else None,
                ]
            log("Loaded " + str(len(Identity.known_destinations)) + " known destinations", LOG_VERBOSE)
        except OSError:
            log("No known destinations file found", LOG_VERBOSE)
        except Exception as e:
            log("Error loading known destinations: " + str(e), LOG_ERROR)

    @staticmethod
    def persist_data():
        Identity.save_known_destinations()

    @staticmethod
    def from_bytes(prv_bytes):
        identity = Identity(create_keys=False)
        if identity.load_private_key(prv_bytes):
            return identity
        return None

    @staticmethod
    def from_file(path):
        identity = Identity(create_keys=False)
        if identity.load(path):
            return identity
        return None

    def __init__(self, create_keys=True):
        self.prv = None
        self.prv_bytes = None
        self.sig_prv = None
        self.sig_prv_bytes = None
        self.pub = None
        self.pub_bytes = None
        self.sig_pub = None
        self.sig_pub_bytes = None
        self.hash = None
        self.hexhash = None
        self.app_data = None

        if create_keys:
            self.create_keys()

    def create_keys(self):
        self.prv = X25519PrivateKey.generate()
        self.prv_bytes = self.prv.private_bytes()
        self.sig_prv = Ed25519PrivateKey.generate()
        self.sig_prv_bytes = self.sig_prv.private_bytes()
        self.pub = self.prv.public_key()
        self.pub_bytes = self.pub.public_bytes()
        self.sig_pub = self.sig_prv.public_key()
        self.sig_pub_bytes = self.sig_pub.public_bytes()
        self.update_hashes()
        log("Identity keys created for " + self.hexhash, LOG_VERBOSE)

    def get_private_key(self):
        return self.prv_bytes + self.sig_prv_bytes

    def get_public_key(self):
        return self.pub_bytes + self.sig_pub_bytes

    def load_private_key(self, prv_bytes):
        try:
            half = Identity.KEYSIZE // 8 // 2
            self.prv_bytes = prv_bytes[:half]
            self.prv = X25519PrivateKey.from_private_bytes(self.prv_bytes)
            self.sig_prv_bytes = prv_bytes[half:]
            self.sig_prv = Ed25519PrivateKey.from_private_bytes(self.sig_prv_bytes)
            self.pub = self.prv.public_key()
            self.pub_bytes = self.pub.public_bytes()
            self.sig_pub = self.sig_prv.public_key()
            self.sig_pub_bytes = self.sig_pub.public_bytes()
            self.update_hashes()
            return True
        except Exception as e:
            log("Failed to load identity key: " + str(e), LOG_ERROR)
            return False

    def load_public_key(self, pub_bytes):
        try:
            half = Identity.KEYSIZE // 8 // 2
            self.pub_bytes = pub_bytes[:half]
            self.sig_pub_bytes = pub_bytes[half:]
            self.pub = X25519PublicKey.from_public_bytes(self.pub_bytes)
            self.sig_pub = Ed25519PublicKey.from_public_bytes(self.sig_pub_bytes)
            self.update_hashes()
        except Exception as e:
            log("Error loading public key: " + str(e), LOG_ERROR)

    def update_hashes(self):
        self.hash = Identity.truncated_hash(self.get_public_key())
        self.hexhash = self.hash.hex()

    def load(self, path):
        try:
            with open(path, "rb") as f:
                return self.load_private_key(f.read())
        except Exception as e:
            log("Error loading identity from " + str(path) + ": " + str(e), LOG_ERROR)
            return False

    def to_file(self, path):
        try:
            with open(path, "wb") as f:
                f.write(self.get_private_key())
            return True
        except Exception as e:
            log("Error saving identity to " + str(path) + ": " + str(e), LOG_ERROR)
            return False

    def get_salt(self):
        return self.hash

    def get_context(self):
        return None

    def encrypt(self, plaintext, ratchet=None):
        if self.pub is not None:
            ephemeral_key = X25519PrivateKey.generate()
            ephemeral_pub_bytes = ephemeral_key.public_key().public_bytes()

            if ratchet is not None:
                target_public_key = X25519PublicKey.from_public_bytes(ratchet)
            else:
                target_public_key = self.pub

            shared_key = ephemeral_key.exchange(target_public_key)
            derived_key = hkdf(
                length=Identity.DERIVED_KEY_LENGTH,
                derive_from=shared_key,
                salt=self.get_salt(),
                context=self.get_context(),
            )
            token = Token(derived_key)
            ciphertext = token.encrypt(plaintext)
            return ephemeral_pub_bytes + ciphertext
        else:
            raise KeyError("No public key for encryption")

    def _do_decrypt(self, shared_key, ciphertext):
        derived_key = hkdf(
            length=Identity.DERIVED_KEY_LENGTH,
            derive_from=shared_key,
            salt=self.get_salt(),
            context=self.get_context(),
        )
        token = Token(derived_key)
        return token.decrypt(ciphertext)

    def decrypt(self, ciphertext_token, ratchets=None, enforce_ratchets=False, ratchet_id_receiver=None):
        if self.prv is not None:
            if len(ciphertext_token) > Identity.KEYSIZE // 8 // 2:
                plaintext = None
                try:
                    half = Identity.KEYSIZE // 8 // 2
                    peer_pub_bytes = ciphertext_token[:half]
                    peer_pub = X25519PublicKey.from_public_bytes(peer_pub_bytes)
                    ciphertext = ciphertext_token[half:]

                    if ratchets:
                        for ratchet in ratchets:
                            try:
                                ratchet_prv = X25519PrivateKey.from_private_bytes(ratchet)
                                ratchet_id = Identity._get_ratchet_id(ratchet_prv.public_key().public_bytes())
                                shared_key = ratchet_prv.exchange(peer_pub)
                                plaintext = self._do_decrypt(shared_key, ciphertext)
                                if ratchet_id_receiver:
                                    ratchet_id_receiver.latest_ratchet_id = ratchet_id
                                break
                            except:
                                pass

                    if enforce_ratchets and plaintext is None:
                        if ratchet_id_receiver:
                            ratchet_id_receiver.latest_ratchet_id = None
                        return None

                    if plaintext is None:
                        shared_key = self.prv.exchange(peer_pub)
                        plaintext = self._do_decrypt(shared_key, ciphertext)
                        if ratchet_id_receiver:
                            ratchet_id_receiver.latest_ratchet_id = None

                except Exception as e:
                    log("Decryption failed: " + str(e), LOG_DEBUG)
                    if ratchet_id_receiver:
                        ratchet_id_receiver.latest_ratchet_id = None

                return plaintext
            else:
                return None
        else:
            raise KeyError("No private key for decryption")

    def sign(self, message):
        if self.sig_prv is not None:
            return self.sig_prv.sign(message)
        else:
            raise KeyError("No private key for signing")

    def validate(self, signature, message):
        if self.sig_pub is not None:
            try:
                self.sig_pub.verify(signature, message)
                return True
            except Exception as e:
                log("Ed25519 verify failed: " + str(e), LOG_DEBUG)
                return False
        else:
            raise KeyError("No public key for validation")

    def __str__(self):
        if self.hexhash:
            return "<" + self.hexhash + ">"
        return "<Identity:no-hash>"
