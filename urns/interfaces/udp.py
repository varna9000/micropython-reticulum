# µReticulum UDP Interface
# WiFi UDP communication for Pico W
# Uses select.poll() for non-blocking async I/O

import socket
import time
from . import Interface
from ..log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE, LOG_EXTREME


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

        self._socket = None

        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Enable broadcast
            try:
                self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except:
                log("Could not enable broadcast on " + self.name, LOG_DEBUG)

            self._socket.bind((self.listen_ip, self.listen_port))
            self._socket.setblocking(False)

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
                # Compute broadcast: IP | ~subnet
                ip_parts = [int(x) for x in ip.split(".")]
                mask_parts = [int(x) for x in subnet.split(".")]
                bcast = ".".join([str(ip_parts[i] | (255 - mask_parts[i])) for i in range(4)])
                log("Auto-detected broadcast: " + bcast, LOG_VERBOSE)
                return bcast
        except Exception as e:
            log("Broadcast auto-detect failed: " + str(e), LOG_DEBUG)
        return "255.255.255.255"

    def process_outgoing(self, data):
        if self.online and self._socket:
            try:
                self._socket.sendto(data, (self.forward_ip, self.forward_port))
                self.txb += len(data)
                self.tx += 1
                self._last_activity = time.time()
                log("UDP sent " + str(len(data)) + "B to " + self.forward_ip + ":" + str(self.forward_port), LOG_DEBUG)
            except Exception as e:
                log("UDP send error: " + str(e), LOG_ERROR)

    async def poll_loop(self):
        """Async poll loop for incoming UDP data"""
        try:
            import uasyncio as asyncio
        except ImportError:
            import asyncio

        log("UDP poll loop started for " + self.name, LOG_VERBOSE)

        loop_count = 0
        while self.online:
            try:
                loop_count += 1
                if loop_count % 3000 == 0:
                    log("UDP poll alive, loops=" + str(loop_count), LOG_DEBUG)

                # Use direct non-blocking recvfrom (more reliable on
                # MicroPython ESP32 than select.poll after sendto)
                try:
                    data, addr = self._socket.recvfrom(self.mtu)
                    if data:
                        log("UDP recv " + str(len(data)) + "B from " + str(addr), LOG_DEBUG)
                        try:
                            self.process_incoming(data)
                        except Exception as e:
                            log("UDP process_incoming error: " + str(e), LOG_ERROR)
                except OSError:
                    pass  # EAGAIN / EWOULDBLOCK - no data available

            except Exception as e:
                log("UDP poll error: " + str(e), LOG_ERROR)

            await asyncio.sleep(0.005)  # 5ms yield

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
