# µReticulum Destination
# Endpoint addressing and announce management

import os
import sys
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

    stamp_announces = False         # set by Reticulum.setup_interfaces (background generation)
    stamp_config = None             # announce_stamps.configure() kwargs, applied when the module first loads
    stamp_dir = None                # override of the stamps DIRECTORY itself (None: <Identity.storagepath>/stamps)
    STAMP_SUBDIR = "/stamps"        # <storage>/stamps/<dest_hex>.dstamp; tools/gen_announce_stamp.py writes the same

    @staticmethod
    def stamps_dir():
        """The one place that says where announce stamps live (announce_stamps
        and the cheap stored-stamp probe both use it)."""
        d = Destination.stamp_dir
        if d is None:
            d = Identity.storagepath + Destination.STAMP_SUBDIR
        return d

    @staticmethod
    def _announce_stamps():
        """The announce_stamps module, imported on first need and configured
        then. Boards that neither generate stamps nor hold a stored one never
        get here, so the module never loads on them."""
        from . import announce_stamps as m
        cfg = Destination.stamp_config
        if cfg is not None:
            Destination.stamp_config = None
            m.configure(**cfg)
        return m

    def _stamp_stored(self):
        """Is there an announce stamp for this destination? Cheap: the module is
        already loaded (it knows), else one os.stat of the stored stamp file."""
        if sys.modules.get(__name__.rsplit(".", 1)[0] + ".announce_stamps") is not None:
            return True
        try:
            os.stat(Destination.stamps_dir() + "/" + self.hexhash + ".dstamp")
            return True
        except Exception:
            return False
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
        self.max_request_size = None    # see set_max_request_size()
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
        self.link_cost = None           # announced link-request stamp cost (None/0: none needed)

        from .transport import Transport
        Transport.register_destination(self)

        if Destination.stamp_announces and type == Destination.SINGLE and direction == Destination.IN:
            try:
                Destination._announce_stamps().request(self)
            except Exception as e:
                log("Announce stamp error: " + repr(e), LOG_ERROR)

    def __str__(self):
        return "<" + self.name + ":" + self.hexhash + ">"

    _emitted = {}               # dest_hash -> last announce emission time (RAM-bounded)
    _stored = {}                # dest_hash -> (value, mode) as persisted in flash; mode 0 real, 1 ceiling
    _EMITTED_MAX = 8
    CLOCK_VALID_MIN = 1700000000    # a wall clock below this is "unset" (2023-11-14)
    PERSIST_EVERY = 600             # flash write at most about once per 10 minutes of emission time
    _RTC_MAGIC = b"UE1"

    # -- exact last value in RTC memory (survives deep sleep and soft reset), no flash wear
    @staticmethod
    def _rtc_read():
        try:
            import machine
            raw = machine.RTC().memory()
        except Exception:
            return None
        try:
            if len(raw) < 6 or raw[0:3] != Destination._RTC_MAGIC:
                return None
            n = raw[3]
            end = 4 + n * 21
            if len(raw) < end + 2 or sum(raw[:end]) & 0xFFFF != int.from_bytes(raw[end:end + 2], "big"):
                return None
            d = {}
            for i in range(n):
                o = 4 + i * 21
                d[bytes(raw[o:o + 16])] = int.from_bytes(raw[o + 16:o + 21], "big")
            return d
        except Exception:
            return None

    @staticmethod
    def _rtc_get(dest_hash):
        d = Destination._rtc_read()
        return d.get(dest_hash) if d else None

    @staticmethod
    def _rtc_put(dest_hash, t):
        d = Destination._rtc_read() or {}
        d[dest_hash] = t
        while len(d) > Destination._EMITTED_MAX:
            for k in d:
                if k != dest_hash:
                    del d[k]
                    break
        try:
            import machine
            buf = bytearray(Destination._RTC_MAGIC + bytes([len(d)]))
            for k in d:
                buf += k + d[k].to_bytes(5, "big")
            buf += (sum(buf) & 0xFFFF).to_bytes(2, "big")
            machine.RTC().memory(bytes(buf))
        except Exception:
            pass

    # -- coarse floor in flash
    @staticmethod
    def _flash_read(dest_hash):
        try:
            with open(Identity.storagepath + "/emitted/" + dest_hash.hex(), "rb") as f:
                raw = f.read(6)
        except Exception:
            return None
        if len(raw) == 5:               # legacy: "every emission is below this horizon"
            return (int.from_bytes(raw, "big") - 1, 1)
        if len(raw) == 6 and raw[0] in (0, 1):
            return (int.from_bytes(raw[1:6], "big"), raw[0])
        return None

    @staticmethod
    def _flash_write(dest_hash, value, mode):
        try:
            import os
            d = Identity.storagepath + "/emitted"
            try:
                os.stat(d)
            except OSError:
                os.mkdir(d)
            with open(d + "/" + dest_hash.hex(), "wb") as f:
                f.write(bytes([mode]) + value.to_bytes(5, "big"))
            return True
        except Exception:
            return False

    @staticmethod
    def _emission_time(dest_hash, now):
        """Emission time for the next announce of dest_hash. Reference nodes only
        adopt an announce whose emission time is newer than the newest they hold, so
        an emission must never be lower than or equal to an earlier one of this
        destination, across reboots and with an unset or stepped-back clock.

        t = now when now is ahead of everything emitted before, else last + 1.
        What "last" is after a boot:
          1. RTC memory (machine.RTC().memory(), survives deep sleep and soft reset):
             the exact last value, no flash involved.
          2. else the flash file <storage>/emitted/<dest_hex> (6 bytes: mode, value).
             mode 0 "real": value was a real-clock emission, and every emission since
             is below value + PERSIST_EVERY. With a valid clock past it (the normal
             case) the real time is emitted - no drift. mode 1 "ceiling": every
             emission is <= value (used while the clock is unset or behind), so the
             next emission starts above it.
        Flash is written only when the emission is not covered by the stored value
        (about once per PERSIST_EVERY = 10 min of emission time), when a real clock
        takes over from a ceiling, and in ceiling mode in blocks of PERSIST_EVERY, so
        a reboot cannot reuse a value. A persisted value in the future of a valid
        clock (an old 5-byte "horizon" file, or an unset-clock run) is continued from
        in +1 steps - peers hold an announce that new, so a lower time would be ignored -
        and the emission falls back to real time as soon as the clock passes it
        (about one announce interval per second of surplus)."""
        D = Destination
        last = D._emitted.get(dest_hash)
        if last is None:
            st = D._flash_read(dest_hash)
            if st is not None:
                D._stored[dest_hash] = st
            else:
                D._stored.pop(dest_hash, None)
            last = D._rtc_get(dest_hash)
            if last is None:
                if st is None:
                    last = 0
                elif st[1] == 0:
                    last = st[0] if now > st[0] else st[0] + D.PERSIST_EVERY - 1
                else:
                    last = st[0]
        t = now if now > last else last + 1
        if t != now and t - now > 1:
            log("Announce clock is behind the last emission (" + str(now) + " <= " + str(last)
                + "); emitting " + str(t) + " so peers still accept it", LOG_NOTICE)
        st = D._stored.get(dest_hash)
        real = now >= D.CLOCK_VALID_MIN and t - now <= 2    # a few announces in one second still count as real time
        if st is None:
            covered = False
        elif st[1] == 0:
            covered = t < st[0] + D.PERSIST_EVERY
        else:
            covered = t <= st[0]
        if not covered or (real and st[1] == 1):
            if real:
                ns = (t, 0)
            else:
                ns = (t + D.PERSIST_EVERY, 1)
            if D._flash_write(dest_hash, ns[0], ns[1]):
                D._stored[dest_hash] = ns   # on failure the next emission tries again
        if len(D._emitted) >= D._EMITTED_MAX and dest_hash not in D._emitted:
            for k in D._emitted:
                del D._emitted[k]
                D._stored.pop(k, None)
                break
        D._emitted[dest_hash] = t
        D._rtc_put(dest_hash, t)
        return t

    def announce(self, app_data=None, path_response=False, attached_interface=None, tag=None, send=True):
        if self.type != Destination.SINGLE:
            raise TypeError("Only SINGLE destinations can be announced")
        if self.direction != Destination.IN:
            raise TypeError("Only IN destinations can be announced")

        import sys
        ratchet = b""
        destination_hash = self.hash
        t = int(time.time())
        if sys.platform == "esp32":
            t += 946684800  # ESP32 MicroPython uses 2000 epoch, convert to Unix
        t = Destination._emission_time(self.hash, t)
        random_hash = Identity.get_random_hash()[0:5] + t.to_bytes(5, "big")

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

        # Reticulum 1.6 announce stamp: appended to app_data, inside the signed
        # data. Only when a stored/generated stamp exists; otherwise the announce
        # goes out unstamped (generation runs in the background).
        ext = None
        try:
            if Destination.stamp_announces or self._stamp_stored():
                ext = Destination._announce_stamps().for_announce(self)
        except Exception as e:
            log("Announce stamp error: " + repr(e), LOG_ERROR)
        if ext is not None:
            # A HEADER_1 announce must leave room for a transport id (16 B)
            # so a relay can re-emit it (reference Packet.pack).
            room = (const.MTU - 19 - 16 - len(self.identity.get_public_key()) - len(self.name_hash)
                    - 10 - len(ratchet) - const.SIGLENGTH // 8)
            need = len(ext) + (len(app_data) if app_data is not None else 0)
            if need > room:
                log("Announce for " + self.hexhash[:8] + " goes out UNSTAMPED: app_data is "
                    + str(need - len(ext)) + " B, a stamped announce allows " + str(room - len(ext))
                    + " B (an enforcing hub will drop it; shorten the display name)", LOG_ERROR)
            else:
                app_data = ext if app_data is None else app_data + ext

        signed_data = self.hash + self.identity.get_public_key() + self.name_hash + random_hash + ratchet
        if app_data is not None:
            signed_data += app_data

        try:
            import gc; gc.collect()
        except:
            pass
        signature = self.identity.sign(signed_data)
        try:
            gc.collect()
        except:
            pass
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
            # Transport.outbound clears .sent when no interface could transmit
            # it: report that instead of pretending the announce went out.
            return bool(announce_packet.sent)
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

    def register_request_handler(self, path, response_generator=None, allow=ALLOW_NONE):
        path_hash = Identity.truncated_hash(path.encode("utf-8"))
        self.request_handlers[path_hash] = {
            "path": path,
            "generator": response_generator,
            "allow": allow,
        }
        log("Registered request handler for " + path, LOG_VERBOSE)

    def set_max_request_size(self, max_request_size):
        """Cap the request size this destination's handlers will accept, in
        bytes (RNS 1.4.1 API). None means no app limit — the stack's own
        MAX_RESOURCE_SIZE ceiling still applies. Worth setting well below that
        on a node whose free heap is measured in tens of KB: a 16 KB request is
        accepted and buffered before any handler sees it."""
        if max_request_size is None:
            self.max_request_size = None
            return
        try:
            max_request_size = int(max_request_size)
        except (TypeError, ValueError):
            raise TypeError("Invalid maximum request size")
        if max_request_size < 0:
            raise ValueError("Maximum request size cannot be negative")
        self.max_request_size = max_request_size

    def receive(self, packet):
        if packet.packet_type == const.PKT_LINKREQUEST:
            if self.accept_link_requests:
                try:
                    from .link import Link
                    import gc; gc.collect()
                    link = Link(self, packet)
                    gc.collect()
                except Exception as e:
                    log("Link creation failed: " + str(e), LOG_ERROR)
        else:
            plaintext = self.decrypt(packet.data)
            if plaintext is None:
                # Never fail silently: an addressed packet that reached this
                # destination but won't decrypt is the single most useful
                # debugging signal there is (stale identity, ratchet mismatch,
                # corrupt payload). Reference RNS logs this too.
                log("Decrypt failed for packet to " + self.hash.hex()[:8]
                    + " (" + str(len(packet.data)) + "B)", LOG_DEBUG)
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

    def set_link_cost(self, cost):
        """Link-request stamp cost announced for this destination (0 = initiators
        need no stamp, the default)."""
        self.link_cost = max(0.0, min(float(cost), 40.0))

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
