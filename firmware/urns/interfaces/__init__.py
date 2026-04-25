# µReticulum Interface Base Class

import time
from ..log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR


class Interface:
    # Interface modes
    MODE_FULL = 0x01
    MODE_POINT_TO_POINT = 0x02
    MODE_ACCESS_POINT = 0x03
    MODE_ROAMING = 0x04
    MODE_BOUNDARY = 0x05
    MODE_GATEWAY = 0x06

    def __init__(self, name="Interface"):
        self.name = name
        self.online = False
        self.enabled = True
        self.mode = Interface.MODE_FULL
        self.bitrate = 0
        self.mtu = 500

        # Statistics
        self.rxb = 0
        self.txb = 0
        self.rx = 0
        self.tx = 0

        # Signal info
        self.rssi = None
        self.snr = None

        self._last_activity = 0

        # IFAC (Interface Access Code) — set up via setup_ifac()
        self.ifac_signing_key = None
        self.ifac_key = None
        self.ifac_size = 0

    def setup_ifac(self, config):
        """Derive IFAC keys from network_name/passphrase in config."""
        from .. import const
        network_name = config.get("networkname")
        passphrase = config.get("passphrase")
        if network_name is None and passphrase is None:
            return

        from ..crypto.hashes import sha256
        from ..crypto.hkdf import hkdf
        from ..crypto import Ed25519PrivateKey
        import gc

        ifac_origin = b""
        if network_name:
            ifac_origin += sha256(network_name.encode("utf-8"))
        if passphrase:
            ifac_origin += sha256(passphrase.encode("utf-8"))

        ifac_origin_hash = sha256(ifac_origin)
        self.ifac_key = hkdf(
            length=64,
            derive_from=ifac_origin_hash,
            salt=const.IFAC_SALT,
        )
        self.ifac_size = config.get("ifac_size", const.IFAC_DEFAULT_SIZE)
        gc.collect()

        # Only need Ed25519 signing key (second 32 bytes of ifac_key)
        self.ifac_signing_key = Ed25519PrivateKey.from_private_bytes(self.ifac_key[32:])
        gc.collect()

        log("IFAC enabled on " + self.name + " (size=" + str(self.ifac_size) + ")", LOG_DEBUG)

    def process_incoming(self, data):
        """Called when data arrives on this interface"""
        self.rxb += len(data)
        self.rx += 1
        self._last_activity = time.time()

        from ..transport import Transport
        Transport.inbound(data, self)

    def ifac_sign(self, data):
        """Apply IFAC signing + masking if configured. Called by subclass process_outgoing."""
        if self.ifac_signing_key is None:
            return data

        import gc
        from ..crypto.hkdf import hkdf

        ifac = self.ifac_signing_key.sign(data)[-self.ifac_size:]
        gc.collect()

        mask = hkdf(length=len(data) + self.ifac_size,
                     derive_from=ifac, salt=self.ifac_key)

        new_raw = bytearray(len(data) + self.ifac_size)
        new_raw[0] = data[0] | 0x80
        new_raw[1] = data[1]
        new_raw[2:2 + self.ifac_size] = ifac
        new_raw[2 + self.ifac_size:] = data[2:]

        isz = self.ifac_size
        new_raw[0] = (new_raw[0] ^ mask[0]) | 0x80
        new_raw[1] = new_raw[1] ^ mask[1]
        for i in range(2 + isz, len(new_raw)):
            new_raw[i] = new_raw[i] ^ mask[i]

        gc.collect()
        log("IFAC signed " + str(len(new_raw)) + "B on " + self.name, LOG_DEBUG)
        return bytes(new_raw)

    def process_outgoing(self, data):
        """Send data out through this interface. Override in subclass."""
        raise NotImplementedError

    def close(self):
        """Shutdown the interface"""
        self.online = False
        self.enabled = False

    def __str__(self):
        return self.name
