# µReticulum UDP Interface
# WiFi UDP communication for ESP32 / Pico W
#
# CRITICAL ESP32 WORKAROUNDS:
# - Separate TX/RX sockets (sendto on polled socket breaks POLLIN)
# - WiFi PM disabled (required for broadcast RX)
# - gc.collect() after packet processing (prevent heap fragmentation)

import socket
import time
from . import Interface
from ..log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_NOTICE, LOG_EXTREME

try:
    import select
except ImportError:
    select = None


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

        self._rx_socket = None
        self._tx_socket = None
        self._poller = None

        # ESP32: Disable WiFi power management to receive broadcast packets
        try:
            import network
            wlan = network.WLAN(network.STA_IF)
            if wlan.active():
                try:
                    wlan.config(pm=0)
                    log("WiFi power management disabled (required for broadcast RX)", LOG_VERBOSE)
                except Exception as e:
                    log("Could not disable WiFi PM: " + str(e), LOG_DEBUG)
        except ImportError:
            pass

        # Free memory before socket creation
        try:
            import gc
            gc.collect()
            log("Pre-socket memory: " + str(gc.mem_free()) + " bytes", LOG_DEBUG)
        except:
            pass

        try:
            # RX socket: bound, polled, NEVER used for sending
            self._rx_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._rx_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self._rx_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except:
                pass
            self._rx_socket.bind((self.listen_ip, self.listen_port))
            self._rx_socket.setblocking(False)

            if select:
                self._poller = select.poll()
                self._poller.register(self._rx_socket, select.POLLIN)

            # TX socket: unbound, used only for sendto(), never polled
            self._tx_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                self._tx_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except:
                pass

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
        if self.online and self._tx_socket:
            try:
                self._tx_socket.sendto(data, (self.forward_ip, self.forward_port))
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
                    log("UDP poll alive, loops=" + str(loop_count) + " rx=" + str(self.rx) + " rxb=" + str(self.rxb), LOG_DEBUG)

                if self._poller:
                    events = self._poller.poll(0)  # non-blocking
                    for sock, event in events:
                        if event & select.POLLIN:
                            try:
                                data, addr = self._rx_socket.recvfrom(self.mtu)
                                if data:
                                    log("UDP recv " + str(len(data)) + "B from " + str(addr), LOG_DEBUG)
                                    self.process_incoming(data)
                                    # Free crypto temporaries after processing
                                    try:
                                        import gc; gc.collect()
                                    except:
                                        pass
                            except Exception as e:
                                if "EAGAIN" not in str(e) and "EWOULDBLOCK" not in str(e):
                                    log("UDP recv error: " + str(e), LOG_DEBUG)
                else:
                    # Fallback without select.poll
                    try:
                        data, addr = self._rx_socket.recvfrom(self.mtu)
                        if data:
                            self.process_incoming(data)
                            try:
                                import gc; gc.collect()
                            except:
                                pass
                    except OSError:
                        pass  # EAGAIN / EWOULDBLOCK

            except Exception as e:
                log("UDP poll error: " + str(e), LOG_ERROR)

            await asyncio.sleep(0.01)  # Yield to event loop

        log("UDP poll loop EXITED for " + self.name, LOG_ERROR)

    def close(self):
        super().close()
        if self._rx_socket:
            try:
                if self._poller:
                    self._poller.unregister(self._rx_socket)
                self._rx_socket.close()
            except:
                pass
        if self._tx_socket:
            try:
                self._tx_socket.close()
            except:
                pass
        self._rx_socket = None
        self._tx_socket = None
        log("UDP Interface " + self.name + " closed", LOG_VERBOSE)
