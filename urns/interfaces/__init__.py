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

    def process_incoming(self, data):
        """Called when data arrives on this interface"""
        self.rxb += len(data)
        self.rx += 1
        self._last_activity = time.time()

        from ..transport import Transport
        Transport.inbound(data, self)

    def process_outgoing(self, data):
        """Send data out through this interface. Override in subclass."""
        raise NotImplementedError

    def close(self):
        """Shutdown the interface"""
        self.online = False
        self.enabled = False

    def __str__(self):
        return self.name
