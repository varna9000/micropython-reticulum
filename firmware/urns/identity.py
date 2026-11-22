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
from .crypto import ed25519 as _ed25519_backend


def _gc_after_crypto():
    """The pure-Python 25519 fallback churns MBs of bignum temporaries per
    operation; collecting right after keeps peak heap bounded on small boards.
    With the native module there is no such garbage and each collect costs
    ~10ms+ of heap scan per call on a populated heap — skip it."""
    if not _ed25519_backend.have_native():
        import gc
        gc.collect()


# Reticulum 1.6 announce extension (last 18 bytes of app_data):
# 0xC1, flags, stamp(14), lr_cost fraction, lr_cost integer (MSB = reserved
# "extended" flag). The stamp is valid when
# sha256(dest_hash + public_key + name_hash + stamp) <= target(cost).
ANNOUNCE_STAMPS = True          # parse/strip the extension (reference STAMPS_ENABLED)
ANNOUNCE_STAMP_COST = 23.5      # required cost (reference Transport.DESTINATION_COST)
_EXT_MARKER = 0xC1
_EXT_LEN = 18
_EXT_STAMP_LEN = 14
_EXT_COST_CAP = 100             # reference Transport.STAMP_COST_CAP


def _split_ext(destination_hash, public_key, name_hash, app_data):
    """Return (app_data, link_cost_fp). When app_data ends in a valid-format
    extension whose stamp meets ANNOUNCE_STAMP_COST, the 18 bytes are stripped
    (empty result -> None) and the announced link-request cost (8.8 fixed
    point) is returned; otherwise app_data is returned untouched with None."""
    if (not ANNOUNCE_STAMPS or not app_data or len(app_data) < _EXT_LEN
            or app_data[-_EXT_LEN] != _EXT_MARKER):
        return app_data, None
    anchor = app_data[-1]
    if anchor & 0x80:
        return app_data, None               # extended flag: format unknown
    fp = ((anchor & 0x7F) << 8) | app_data[-2]
    if fp > _EXT_COST_CAP * 256:
        return app_data, None
    try:
        from . import stamp_check
        stamp = app_data[-_EXT_LEN + 2:-_EXT_LEN + 2 + _EXT_STAMP_LEN]
        if not stamp_check.stamp_valid(stamp, ANNOUNCE_STAMP_COST, destination_hash + public_key + name_hash):
            return app_data, None
    except Exception as e:
        log("Announce stamp parsing failed: " + str(e), LOG_ERROR)
        return app_data, None
    app_data = app_data[:-_EXT_LEN]
    return (app_data if len(app_data) else None), fp


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
    forget_hooks = []             # callables(dest_hash): drop state keyed by a destination removed by the cleanup
    _clean_at = 0                 # when the current/last cleanup pass started
    _clean_pend = None            # keys the running pass still has to look at
    _clean_removed = 0
    _kd_dirty = False             # removed something since the last save
    _kd_saved = 0
    storagepath = "/rns"  # Set by Reticulum.__init__

    @staticmethod
    def _evict_oldest(table, ts_idx):
        """Delete the entry with the oldest timestamp (entry[ts_idx]) that no conversation
        is using; False when every entry is in use. One min-scan per insert (~10ms at the
        cap): a batched sorted() amortizes to less total work but spikes >100ms in one tick,
        which reads as UI jank on the T-Deck."""
        try:
            from .transport import Transport
            in_use = Transport.is_in_use
        except Exception:
            in_use = None
        oldest = None
        oldest_ts = None
        for k in table:
            ts = table[k][ts_idx]
            if oldest_ts is not None and ts >= oldest_ts:
                continue
            if in_use is not None and in_use(k):
                continue            # a peer we are using keeps its key
            oldest_ts = ts
            oldest = k
        if oldest is None:
            return False
        del table[oldest]
        return True

    @staticmethod
    def mark_used(destination_hash):
        """We used this destination (sent to it, delivered from it): note the time in the
        entry's 6th element (0/absent = never used, > 0 = last use, -1 = retained)."""
        e = Identity.known_destinations.get(destination_hash)
        if e is None or (len(e) > 5 and e[5] is not None and e[5] < 0):
            return
        while len(e) < 6:
            e.append(None)
        e[5] = time.time()

    @staticmethod
    def retain_destination(destination_hash, retain=True):
        """Keep (or stop keeping) a destination, e.g. a contact, out of the cleanup.
        Returns False when the destination is not known (yet)."""
        e = Identity.known_destinations.get(destination_hash)
        if e is None:
            return False
        while len(e) < 6:
            e.append(None)
        e[5] = -1 if retain else time.time()
        return True

    @staticmethod
    def clean_step(now, budget=const.KD_CLEAN_BATCH):
        """Regular cleanup, after reference Identity.clean_known_destinations: remove a known
        destination that is not retained, not ours, not in use and has no path when it was
        never used and last announced over KD_UNUSED_LINGER ago, or was used but not for over
        1.25 x the path timeout; its ratchet and any state registered in forget_hooks go with
        it. Also drops expired ratchets and ratchets whose destination is gone. Call every job
        tick: a pass starts every KD_CLEAN_INTERVAL and looks at `budget` entries per call, so
        the event loop is never held for long. Returns the number of entries removed."""
        kd = Identity.known_destinations
        kr = Identity.known_ratchets
        pend = Identity._clean_pend
        if pend is None:
            if now - Identity._clean_at < const.KD_CLEAN_INTERVAL:
                return 0
            Identity._clean_at = now
            if not Identity._kd_saved:
                Identity._kd_saved = now
            Identity._clean_removed = 0
            pend = Identity._clean_pend = list(kd) + [k for k in kr if k not in kd]
        from .transport import Transport
        own = [d.hash for d in Transport.destinations]
        n = 0
        while pend and n < budget:
            n += 1
            dh = pend.pop()
            e = kd.get(dh)
            r = kr.get(dh)
            if r is not None and now - r[1] > const.RATCHET_EXPIRY:
                del kr[dh]
                r = None
            if (e is None and r is None) or dh in own or Transport.is_in_use(dh) or Transport.has_path(dh):
                continue
            if e is not None:
                lu = e[5] if len(e) > 5 and e[5] else 0
                if lu < 0:
                    continue                    # retained
                stale = (now - e[0] > const.KD_UNUSED_LINGER) if lu == 0 else (now - lu > const.KD_PATH_TIMEOUT * 1.25)
            else:
                stale = now - r[1] > const.KD_UNUSED_LINGER     # ratchet whose destination is gone
            if stale:
                kd.pop(dh, None)
                kr.pop(dh, None)
                Transport._seen_blob.pop(dh, None)
                for h in Identity.forget_hooks:
                    try:
                        h(dh)
                    except Exception:
                        pass
                Identity._clean_removed += 1
        if not pend:
            Identity._clean_pend = None
            if Identity._clean_removed:
                Identity._kd_dirty = True
                log("Cleaned " + str(Identity._clean_removed) + " stale known destinations", LOG_DEBUG)
            if Identity._kd_dirty and now - Identity._kd_saved >= const.KD_SAVE_INTERVAL:
                Identity._kd_dirty = False
                Identity._kd_saved = now
                Identity.save_known_destinations()
            return Identity._clean_removed
        return 0

    @staticmethod
    def remember(packet_hash, destination_hash, public_key, app_data=None, link_cost_fp=None):
        if len(public_key) != Identity.KEYSIZE // 8:
            raise TypeError("Invalid public key size: " + str(len(public_key)))
        kd = Identity.known_destinations
        if (destination_hash not in kd
                and len(kd) >= const.MAX_KNOWN_DESTINATIONS):
            # LRU-by-announce eviction (the timestamp refreshes on every
            # announce). Unbounded growth on a public hub reached 449 entries
            # / 123KB JSON — a 4.5s flash write at persist and an ever-growing
            # boot load.
            if not Identity._evict_oldest(kd, 0):
                log("known_destinations full of in-use entries, not recording", LOG_DEBUG)
                return
            log("known_destinations at cap, evicted oldest", LOG_DEBUG)
        # [timestamp, packet_hash, public_key, app_data, link_cost_fp]. The link
        # cost rides in the entry, so it shares the table cap and eviction.
        entry = [time.time(), packet_hash, public_key, app_data, link_cost_fp]
        old = kd.get(destination_hash)
        if old is not None and len(old) > 5:
            entry.append(old[5])            # a re-announce keeps "last used" / retained
        kd[destination_hash] = entry

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
    def recall_link_cost(destination_hash):
        """Link-request stamp cost (float) the destination announced, or None
        when unknown / no stamped announce seen."""
        e = Identity.known_destinations.get(destination_hash)
        if e is None or len(e) < 5 or e[4] is None:
            return None
        return e[4] / 256

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
            # Re-announces refresh the received time, like upstream. Bounded like
            # known_destinations: every ratcheted destination on a busy hub used to add an
            # entry for good (30 days expiry is only checked on use).
            kr = Identity.known_ratchets
            if destination_hash not in kr and len(kr) >= const.MAX_KNOWN_DESTINATIONS:
                Identity._evict_oldest(kr, 1)
            kr[destination_hash] = (ratchet, time.time())
        except Exception as e:
            log("Could not remember ratchet: " + str(e), LOG_ERROR)

    @staticmethod
    def get_ratchet(destination_hash):
        entry = Identity.known_ratchets.get(destination_hash)
        if entry:
            ratchet, received = entry
            if time.time() < received + const.RATCHET_EXPIRY:
                return ratchet
            del Identity.known_ratchets[destination_hash]
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

            log("Announce validate: data=" + str(len(packet.data)) + "B hdr=" + str(packet.header_type) + " ctx_flag=" + str(packet.context_flag) + " ctx=" + str(packet.context), LOG_DEBUG)

            base = keysize + name_hash_len + 10
            has_ratchet = packet.context_flag == const.FLAG_SET
            name_hash = packet.data[keysize:keysize + name_hash_len]
            random_hash = packet.data[keysize + name_hash_len:keysize + name_hash_len + 10]
            if has_ratchet:
                ratchet = packet.data[base:base + ratchetsize]
                signature = packet.data[base + ratchetsize:base + ratchetsize + sig_len]
                app_data = b""
                if len(packet.data) > base + ratchetsize + sig_len:
                    app_data = packet.data[base + ratchetsize + sig_len:]
            else:
                ratchet = b""
                signature = packet.data[base:base + sig_len]
                app_data = b""
                if len(packet.data) > base + sig_len:
                    app_data = packet.data[base + sig_len:]

            log("Announce fields: ratchet=" + str(len(ratchet)) + " sig=" + str(len(signature)) + " app=" + str(len(app_data)), LOG_DEBUG)

            if len(signature) != sig_len:
                log("Announce rejected: bad sig length " + str(len(signature)) + " (expected " + str(sig_len) + ")", LOG_DEBUG)
                return False

            signed_data = destination_hash + public_key + name_hash + random_hash + ratchet + app_data

            if not len(packet.data) > keysize + name_hash_len + 10 + sig_len:
                app_data = None

            # Fast path: skip expensive Ed25519 verify (~24s on ESP32) for
            # already-known destinations whose public key matches.  The
            # signature was validated on first receipt; an attacker cannot
            # forge a new announce for the same dest hash without a hash
            # collision on the truncated identity hash.
            if not only_validate_signature and destination_hash in Identity.known_destinations:
                cached = Identity.known_destinations[destination_hash]
                if cached[2] == public_key:
                    fast_app, lr_fp = _split_ext(destination_hash, public_key, name_hash, app_data)
                    stored_fp = cached[4] if len(cached) > 4 else None
                    if lr_fp == stored_fp:
                        log("Announce from known dest " + destination_hash.hex()[:8] + ", skip verify", LOG_VERBOSE)
                        Identity.remember(packet.get_hash(), destination_hash, public_key, fast_app, lr_fp)
                        if ratchet:
                            Identity._remember_ratchet(destination_hash, ratchet)
                        return True
                    # The announced link-request stamp cost changed (or
                    # appeared/vanished): never take it on the unsigned fast
                    # path, a replayed or forged announce could set any cost.
                    log("Announce from known dest " + destination_hash.hex()[:8]
                        + " changes the link cost, verifying signature", LOG_VERBOSE)

            announced_identity = Identity(create_keys=False)
            announced_identity.load_public_key(public_key)
            log("Announce identity loaded: hash=" + str(announced_identity.hexhash), LOG_DEBUG)

            sig_valid = announced_identity.validate(signature, signed_data)
            _gc_after_crypto()
            log("Announce sig_valid=" + str(sig_valid), LOG_DEBUG)

            # Fallback: if verification failed, try opposite ratchet assumption.
            # Some transport paths or older Reticulum versions may encode the
            # context_flag differently, causing ratchet field misalignment.
            if not sig_valid:
                alternate_layout = False
                if has_ratchet:
                    log("Ratchet path failed, retrying without ratchet", LOG_DEBUG)
                    ratchet = b""
                    signature = packet.data[base:base + sig_len]
                    app_data = b""
                    if len(packet.data) > base + sig_len:
                        app_data = packet.data[base + sig_len:]
                    alternate_layout = True
                elif len(packet.data) >= base + ratchetsize + sig_len:
                    log("No-ratchet path failed, retrying with ratchet", LOG_DEBUG)
                    ratchet = packet.data[base:base + ratchetsize]
                    signature = packet.data[base + ratchetsize:base + ratchetsize + sig_len]
                    app_data = b""
                    if len(packet.data) > base + ratchetsize + sig_len:
                        app_data = packet.data[base + ratchetsize + sig_len:]
                    alternate_layout = True
                if alternate_layout and len(signature) == sig_len:
                    signed_data = destination_hash + public_key + name_hash + random_hash + ratchet + app_data
                    sig_valid = announced_identity.validate(signature, signed_data)
                    _gc_after_crypto()
                    if sig_valid:
                        log("Announce verified with alternate layout", LOG_DEBUG)
                if not len(packet.data) > keysize + name_hash_len + 10 + sig_len:
                    app_data = None

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

                    # Signature was checked over the full, unstripped app_data.
                    app_data, lr_fp = _split_ext(destination_hash, public_key, name_hash, app_data)
                    Identity.remember(packet.get_hash(), destination_hash, public_key, app_data, lr_fp)

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
                    entry[4] if len(entry) > 4 else None,  # link cost (8.8 fixed point)
                ]
                if len(entry) > 5:
                    data[key_hex].append(entry[5])      # last used / -1 retained
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
                    entry[4] if len(entry) > 4 else None,
                ]
                if len(entry) > 5:
                    Identity.known_destinations[dh].append(entry[5])
            # Trim a file written before the cap existed (or by a bigger
            # board) down to this board's cap, oldest first.
            kd = Identity.known_destinations
            if len(kd) > const.MAX_KNOWN_DESTINATIONS:
                by_age = sorted(kd, key=lambda k: kd[k][0])
                for k in by_age[:len(kd) - const.MAX_KNOWN_DESTINATIONS]:
                    del kd[k]
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
            _gc_after_crypto()

            if ratchet is not None:
                target_public_key = X25519PublicKey.from_public_bytes(ratchet)
            else:
                target_public_key = self.pub

            shared_key = ephemeral_key.exchange(target_public_key)
            _gc_after_crypto()
            derived_key = hkdf(
                length=Identity.DERIVED_KEY_LENGTH,
                derive_from=shared_key,
                salt=self.get_salt(),
                context=self.get_context(),
            )
            token = Token(derived_key)
            _gc_after_crypto()
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
