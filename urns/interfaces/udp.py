# µReticulum UDP Interface
# WiFi UDP communication for ESP32 / Pico W
#
# ESP32 WORKAROUNDS:
# - Single socket for TX+RX (saves ~280 bytes IDF heap for lwIP pbufs)
# - settimeout(0) re-asserted after every sendto (ESP32 lwIP bug: sendto
#   corrupts non-blocking state, causing recvfrom to block)
# - WiFi PM disabled (required for broadcast RX)
# - AP_IF deactivated in example_node.py (dual-interface blocks broadcast RX)

import socket
import time
from . import Interface
from ..log import log, LOG_VERBOSE, LOG_DEBUG, LOG_WARNING, LOG_ERROR, LOG_NOTICE, LOG_EXTREME

import gc

def _idf_free():
    """Return (free, largest_contiguous) from main IDF heap region, or None on non-ESP32."""
    try:
        import esp32
        best = None
        for t in esp32.idf_heap_info(esp32.HEAP_DATA):
            if best is None or t[0] > best[0]:
                best = t
        if best:
            return (best[1], best[2])
    except:
        pass
    return None


class UDPInterface(Interface):
    def __init__(self, config):
        name = config.get("name", "UDP")
        super().__init__(name)

        self.listen_ip = config.get("listen_ip", "0.0.0.0")
        self.listen_port = config.get("listen_port", 4242)
        self.forward_ip = config.get("forward_ip", None)
        self.forward_port = config.get("forward_port", 4242)
        self.bitrate = config.get("bitrate", 10000000)  # ~10Mbps WiFi

        # Auto-detect subnet broadcast if not specified
        if self.forward_ip is None or self.forward_ip == "255.255.255.255":
            self.forward_ip = self._detect_broadcast()

        # Pre-resolve address once
        self._forward_addr = socket.getaddrinfo(self.forward_ip, self.forward_port)[0][-1]

        self._socket = None

        # ESP32: Disable WiFi power management to receive broadcast packets
        try:
            import network
            wlan = network.WLAN(network.STA_IF)
            if wlan.active():
                try:
                    wlan.config(pm=0)
                    log("WiFi power management disabled (required for broadcast RX)", LOG_VERBOSE)
                except Exception as e:
                    log("WARNING: Could not disable WiFi PM, broadcast RX may fail: " + str(e), LOG_WARNING)
        except ImportError:
            pass

        gc.collect()
        idf = _idf_free()
        if idf:
            log("IDF before socket: free=" + str(idf[0]) + " largest=" + str(idf[1]), LOG_DEBUG)

        try:
            # Single socket for TX+RX — saves ~280 bytes IDF vs two sockets.
            # settimeout(0) re-asserted after each sendto to work around ESP32
            # lwIP bug where sendto corrupts non-blocking state.
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except Exception as e:
                log("WARNING: SO_BROADCAST failed: " + str(e) + " — broadcast may not work", LOG_WARNING)
            self._socket.bind((self.listen_ip, self.listen_port))
            self._socket.settimeout(0)

            gc.collect()
            idf = _idf_free()
            if idf:
                log("IDF after socket: free=" + str(idf[0]) + " largest=" + str(idf[1]), LOG_DEBUG)

            self.online = True
            log("UDP Interface " + self.name + " listening on " + self.listen_ip + ":" + str(self.listen_port), LOG_NOTICE)
            log("UDP Interface " + self.name + " broadcasting to " + self.forward_ip + ":" + str(self.forward_port), LOG_VERBOSE)

        except Exception as e:
            log("Could not create UDP interface: " + str(e), LOG_ERROR)
            self.online = False

    @staticmethod
    def _detect_broadcast():
        """Auto-detect subnet broadcast address from network interface."""
        try:
            import network
            wlan = network.WLAN(network.STA_IF)
            if wlan.active() and wlan.isconnected():
                ip, subnet, gateway, dns = wlan.ifconfig()
                ip_parts = [int(x) for x in ip.split(".")]
                mask_parts = [int(x) for x in subnet.split(".")]
                bcast = ".".join([str(ip_parts[i] | (255 - mask_parts[i])) for i in range(4)])
                log("Auto-detected broadcast: " + bcast, LOG_VERBOSE)
                return bcast
        except Exception as e:
            log("Broadcast auto-detect failed: " + str(e), LOG_DEBUG)
        return "255.255.255.255"

    def process_outgoing(self, data):
        if not self.online or not self._socket:
            return False

        gc.collect()

        idf = _idf_free()
        if idf:
            log("TX pre-send: idf_free=" + str(idf[0]) + " largest=" + str(idf[1]) + " data=" + str(len(data)) + "B", LOG_DEBUG)

        sent = False
        try:
            self._socket.sendto(data, self._forward_addr)
            # Re-assert non-blocking after sendto — ESP32 lwIP bug:
            # sendto corrupts the socket's non-blocking state
            self._socket.settimeout(0)
            self.txb += len(data)
            self.tx += 1
            self._last_activity = time.time()
            log("UDP sent " + str(len(data)) + "B", LOG_DEBUG)
            sent = True
        except Exception as e:
            log("UDP send error: " + str(e), LOG_ERROR)
            # Re-assert non-blocking even on error
            try:
                self._socket.settimeout(0)
            except:
                pass

        gc.collect()
        return sent

    async def poll_loop(self):
        """Async poll loop for incoming UDP data."""
        try:
            import uasyncio as asyncio
        except ImportError:
            import asyncio

        log("UDP poll loop started for " + self.name, LOG_NOTICE)
        gc.collect()
        idf = _idf_free()
        if idf:
            log("IDF at poll start: free=" + str(idf[0]) + " largest=" + str(idf[1]), LOG_DEBUG)

        loop_count = 0
        _err_count = 0
        while self.online:
            try:
                loop_count += 1
                if loop_count % 1000 == 0:
                    idf = _idf_free()
                    idf_str = (" idf=" + str(idf[0]) + "/" + str(idf[1])) if idf else ""
                    log("UDP poll alive, loops=" + str(loop_count)
                        + " rx=" + str(self.rx) + " rxb=" + str(self.rxb)
                        + " sock=" + str(self._socket is not None)
                        + idf_str, LOG_VERBOSE)

                if not self._socket:
                    await asyncio.sleep(0.05)
                    continue

                try:
                    data, addr = self._socket.recvfrom(self.mtu)
                    if data:
                        log("UDP recv " + str(len(data)) + "B from " + str(addr), LOG_DEBUG)
                        self.process_incoming(data)
                        gc.collect()
                except OSError as e:
                    # Log non-EAGAIN errors (errno 11) — they indicate real problems
                    eno = e.args[0] if e.args else 0
                    if eno != 11:
                        _err_count += 1
                        if _err_count <= 5:
                            log("UDP recvfrom errno=" + str(eno) + ": " + str(e), LOG_WARNING)
            except Exception as e:
                log("UDP poll error: " + str(e), LOG_ERROR)

            await asyncio.sleep(0.01)  # Yield to event loop

        log("UDP poll loop EXITED for " + self.name, LOG_ERROR)

    def close(self):
        super().close()
        if self._socket:
            try:
                self._socket.close()
            except:
                pass
        self._socket = None
        log("UDP Interface " + self.name + " closed", LOG_VERBOSE)
