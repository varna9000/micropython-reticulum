# µReticulum UDP Interface
# WiFi UDP communication for ESP32 / Pico W
#
# lwIP WORKAROUNDS (still needed on ESP32-S3):
# - settimeout(0) re-asserted after every sendto (lwIP bug: sendto
#   corrupts non-blocking state, causing recvfrom to block)
# - RX watchdog: if TX works but RX is dead, recreate socket

import socket
import time
from . import Interface
from ..log import log, LOG_VERBOSE, LOG_DEBUG, LOG_WARNING, LOG_ERROR, LOG_NOTICE, LOG_EXTREME

import gc


def _resolve_addr(host, port):
    """Resolve address with fallback to raw tuple if getaddrinfo fails."""
    for attempt in range(3):
        try:
            return socket.getaddrinfo(host, port)[0][-1]
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
            else:
                log("getaddrinfo failed, using raw tuple: " + str(e), LOG_WARNING)
                return (host, port)


def _create_socket(listen_ip, listen_port):
    """Create a bound, non-blocking UDP socket with broadcast enabled."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except Exception as e:
        log("SO_BROADCAST failed: " + str(e), LOG_WARNING)
    sock.bind((listen_ip, listen_port))
    sock.settimeout(0)
    return sock


class UDPInterface(Interface):
    # Watchdog: if we've transmitted but received nothing for this many
    # seconds, assume the RX socket is broken and recreate it.
    RX_WATCHDOG_TIMEOUT = 60
    RX_WATCHDOG_MAX_RETRIES = 8

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

        self._forward_addr = _resolve_addr(self.forward_ip, self.forward_port)

        self._socket = None
        self._first_tx_time = 0
        self._last_rx_time = 0
        self._watchdog_retries = 0

        try:
            self._socket = _create_socket(self.listen_ip, self.listen_port)

            self.online = True
            log("UDP " + self.name + " on " + self.listen_ip + ":" + str(self.listen_port), LOG_NOTICE)
            log("UDP " + self.name + " broadcast to " + self.forward_ip + ":" + str(self.forward_port), LOG_VERBOSE)

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

    def _recreate_socket(self):
        """Close and recreate the socket. Used by the watchdog when RX is stuck."""
        log("Recreating socket (watchdog)", LOG_WARNING)
        if self._socket:
            try:
                self._socket.close()
            except:
                pass
            self._socket = None

        gc.collect()

        try:
            self._socket = _create_socket(self.listen_ip, self.listen_port)
            log("Socket recreated", LOG_NOTICE)
            return True
        except Exception as e:
            log("Socket recreation failed: " + str(e), LOG_ERROR)
            return False

    def process_outgoing(self, data):
        if not self.online or not self._socket:
            return False

        sent = False
        try:
            data = self.ifac_sign(data)
            self._socket.sendto(data, self._forward_addr)
            # Re-assert non-blocking after sendto — lwIP bug:
            # sendto corrupts the socket's non-blocking state
            self._socket.settimeout(0)
            self.txb += len(data)
            self.tx += 1
            self._last_activity = time.time()
            if self._first_tx_time == 0:
                self._first_tx_time = self._last_activity
            log("UDP sent " + str(len(data)) + "B", LOG_DEBUG)
            sent = True
        except Exception as e:
            log("UDP send error: " + str(e), LOG_ERROR)
            try:
                self._socket.settimeout(0)
            except:
                pass

        return sent

    async def poll_loop(self):
        """Async poll loop for incoming UDP data with RX watchdog."""
        import uasyncio as asyncio

        log("UDP poll loop started for " + self.name, LOG_NOTICE)

        loop_count = 0
        _err_count = 0
        _last_gc = time.time()
        while self.online:
            try:
                loop_count += 1
                now = time.time()

                # --- Periodic GC ---
                if now - _last_gc >= 10:
                    gc.collect()
                    _last_gc = now

                if loop_count % 1000 == 0:
                    log("UDP poll alive, loops=" + str(loop_count)
                        + " rx=" + str(self.rx) + " rxb=" + str(self.rxb)
                        + " tx=" + str(self.tx)
                        + " sock=" + str(self._socket is not None), LOG_VERBOSE)

                # --- RX watchdog ---
                # Trigger when TX has happened but no RX for too long.
                # Uses _last_rx_time (not just rx==0) so it also catches
                # the case where RX dies mid-session.
                _rx_ref = self._last_rx_time if self._last_rx_time > 0 else self._first_tx_time
                if (self._first_tx_time > 0
                        and self._watchdog_retries < self.RX_WATCHDOG_MAX_RETRIES
                        and (now - _rx_ref) > self.RX_WATCHDOG_TIMEOUT):
                    self._watchdog_retries += 1
                    log("RX watchdog: tx=" + str(self.tx)
                        + " rx=" + str(self.rx) + " silent "
                        + str(int(now - _rx_ref)) + "s, retry "
                        + str(self._watchdog_retries) + "/" + str(self.RX_WATCHDOG_MAX_RETRIES),
                        LOG_WARNING)
                    if self._recreate_socket():
                        if self._last_rx_time > 0:
                            self._last_rx_time = now
                        else:
                            self._first_tx_time = now
                        _err_count = 0

                if not self._socket:
                    await asyncio.sleep(0.05)
                    continue

                try:
                    data, addr = self._socket.recvfrom(self.mtu)
                    if data:
                        log("UDP recv " + str(len(data)) + "B from " + str(addr), LOG_DEBUG)
                        self.process_incoming(data)
                        self._last_rx_time = now
                        self._watchdog_retries = 0
                        gc.collect()
                except OSError as e:
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
        log("UDP " + self.name + " closed", LOG_VERBOSE)
