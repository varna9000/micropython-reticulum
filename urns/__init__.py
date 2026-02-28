# µReticulum - MicroPython port of the Reticulum Network Stack
# For ESP32-S3 / Raspberry Pi Pico W

__version__ = "0.1.0"

from .log import log, LOG_NONE, LOG_CRITICAL, LOG_ERROR, LOG_WARNING
from .log import LOG_NOTICE, LOG_INFO, LOG_VERBOSE, LOG_DEBUG, LOG_EXTREME
from . import const
from .identity import Identity
from .destination import Destination
from .packet import Packet, PacketReceipt
from .transport import Transport
from . import lxmf
from .reticulum import Reticulum


def hexrep(data, delimit=True):
    try:
        iter(data)
    except TypeError:
        data = [data]
    d = ":" if delimit else ""
    return d.join("{:02x}".format(c) for c in data)


def prettyhexrep(data):
    return "<" + "".join("{:02x}".format(c) for c in data) + ">"
