# µReticulum Transport
# Supports optional transport mode (blind flood forwarding between interfaces)
# Uses uasyncio instead of threading

import os
import sys
import time
from . import const
from .log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_EXTREME, LOG_NOTICE, LOG_WARNING

# ESP32 MicroPython counts seconds from 2000-01-01; convert to/from Unix epoch.
_EPOCH_OFFSET = 946684800 if sys.platform == "esp32" else 0
# Single sanity floor: a clock/timestamp at/above this is considered "real".
# Below it, the source (or our own clock) is still unset. This also keeps the
# 2000-epoch conversion non-negative. The upper bound is handled by requiring
# corroboration from multiple nodes rather than a hardcoded max.
_TIME_FLOOR = 1704067200       # 2024-01-01 UTC

# Path requests (wire-compatible with reference RNS). A leaf cannot relay, but it
# can ASK a transport node for a route to a destination it has no path to yet.
_PATH_APP_NAME = "rnstransport"     # well-known: "rnstransport.path.request"
_PATH_REQUEST_TIMEOUT = 15          # seconds before a waiting send gives up
_PATH_REREQUEST_INTERVAL = 5        # seconds between re-requests while waiting

# Transport types (module-level for import compatibility)
BROADCAST  = const.TRANSPORT_BROADCAST
TRANSPORT  = const.TRANSPORT_TRANSPORT
RELAY      = const.TRANSPORT_RELAY
TUNNEL     = const.TRANSPORT_TUNNEL


class Transport:
    BROADCAST  = const.TRANSPORT_BROADCAST
    TRANSPORT  = const.TRANSPORT_TRANSPORT

    owner = None
    identity = None
    interfaces = []
    destinations = []
    pending_links = []
    active_links = []
    packet_hashlist = []
    receipts = []
    announce_table = {}
    destination_table = {}
    path_table = {}          # dest_hash -> transport_id (from HDR_2 announces)
    blackholed_identities = []

    # Reachability + on-demand path resolution (path requests). Membership-only,
    # no expiry — see has_path(). path_table is the HDR_2-route subset of this.
    reachable_destinations = {}   # dest_hash -> last announce time (capped)
    _path_waiters = {}            # dest_hash -> [ {on_found, on_timeout, deadline} ]
    _path_request_times = {}      # dest_hash -> last request sent (rate-limit)
    _path_request_dest = None     # cached OUT/PLAIN "rnstransport.path.request"

    transport_enabled = False

    # Time sync (for nodes with no RTC/NTP, e.g. pure-LoRa). Set from config
    # in Reticulum.setup_interfaces(). trusted set holds lowercase hex LXMF
    # delivery hashes. With trusted nodes set, one matching source is enough
    # (authority mode). With no trusted nodes, require min_sources distinct
    # peers whose clock offsets agree within tolerance (corroboration mode).
    time_sync_enabled = False
    time_sync_trusted = set()
    time_sync_min_sources = 2
    time_sync_tolerance = 120    # seconds
    _clock_synced = False
    _time_votes = {}             # source_hex -> clock offset (peer_unix - our_unix)

    _jobs_running = False
    _last_job = 0

    @staticmethod
    def start(owner):
        Transport.owner = owner
        Transport.identity = owner.identity
        Transport.transport_enabled = owner.config.get("enable_transport", False)
        Transport._jobs_running = True
        if Transport.transport_enabled:
            log("Transport engine started — TRANSPORT MODE", LOG_NOTICE)
        else:
            log("Transport engine started", LOG_VERBOSE)

    @staticmethod
    def stop():
        Transport._jobs_running = False
        log("Transport engine stopped", LOG_VERBOSE)

    @staticmethod
    def register_destination(destination):
        dest_hash = destination.hash
        # Cap table size
        if len(Transport.destinations) >= const.MAX_DESTINATIONS:
            log("Destination table full, cannot register", LOG_WARNING)
            return
        # Avoid duplicates
        for d in Transport.destinations:
            if d.hash == dest_hash:
                return
        Transport.destinations.append(destination)

    @staticmethod
    def deregister_destination(destination):
        if destination in Transport.destinations:
            Transport.destinations.remove(destination)

    @staticmethod
    def register_interface(interface):
        if interface not in Transport.interfaces:
            Transport.interfaces.append(interface)
            log("Interface registered: " + str(interface), LOG_VERBOSE)

    @staticmethod
    def deregister_interface(interface):
        if interface in Transport.interfaces:
            Transport.interfaces.remove(interface)

    @staticmethod
    def outbound(packet):
        """Send a packet out through appropriate interfaces"""
        sent = False
        raw = packet.raw

        if not packet.sent:
            packet.sent = True
            packet.sent_at = time.time()

            log("TX " + str(len(raw)) + "B type=" + str(packet.packet_type) + " ifaces=" + str(len(Transport.interfaces)), LOG_DEBUG)

            for interface in Transport.interfaces:
                if interface.online:
                    if packet.attached_interface is not None and interface is not packet.attached_interface:
                        continue
                    try:
                        result = interface.process_outgoing(raw)
                        if result or result is None:
                            sent = True
                            log("TX sent on " + interface.name, LOG_DEBUG)
                        else:
                            log("TX failed on " + interface.name, LOG_WARNING)
                    except Exception as e:
                        log("Error sending on " + str(interface) + ": " + str(e), LOG_ERROR)

            if sent:
                packet.receipt = Transport._create_receipt(packet)
                Transport._cache_packet_hash(packet)
            else:
                log("No interfaces could send packet (registered: " + str(len(Transport.interfaces)) + ")", LOG_ERROR)
                packet.sent = False

        return sent

    @staticmethod
    def _create_receipt(packet):
        if packet.create_receipt:
            from .packet import PacketReceipt
            receipt = PacketReceipt(packet)
            if len(Transport.receipts) >= const.MAX_RECEIPTS:
                Transport.receipts.pop(0)
            Transport.receipts.append(receipt)
            return receipt
        return None

    @staticmethod
    def _cache_packet_hash(packet):
        packet_hash = packet.get_hash()
        if len(Transport.packet_hashlist) >= 256:
            Transport.packet_hashlist.pop(0)
        Transport.packet_hashlist.append(packet_hash)

    @staticmethod
    def _forward(raw, receiving_interface):
        """Forward raw packet to all interfaces except the one it arrived on.

        Announces are rewritten from HDR_1 to HDR_2 with the transport
        node's identity hash as transport_id, so downstream nodes learn the
        transport path back (matching reference RNS Transport behaviour).
        """
        hops = raw[1]
        if hops >= const.TRANSPORT_HOPLIMIT:
            log("Forward: hop limit reached (" + str(hops) + "), dropping", LOG_DEBUG)
            return

        flags = raw[0]
        packet_type = flags & 0x03
        is_hdr1 = (flags & 0x40) == 0x00

        # Rewrite HDR_1 announces as HDR_2 with our transport_id
        if packet_type == const.PKT_ANNOUNCE and is_hdr1 and Transport.identity:
            transport_id = Transport.identity.hash
            # New flags: set HDR_2 (bit 6) + TRANSPORT (bit 4)
            new_flags = (flags | 0x50)
            # HDR_2 format: flags(1) + hops(1) + transport_id(16) + dest_hash(16) + context(1) + data
            fwd = bytearray(bytes([new_flags, hops + 1]) + transport_id + raw[2:])
            log("Forward announce as HDR_2: transport=" + transport_id.hex()[:8], LOG_DEBUG)
        else:
            fwd = bytearray(raw)
            fwd[1] = hops + 1

        for interface in Transport.interfaces:
            if interface is receiving_interface:
                continue
            if not interface.online:
                continue
            try:
                interface.process_outgoing(bytes(fwd))
                log("Forward: " + str(len(fwd)) + "B " + receiving_interface.name + " -> " + interface.name + " hops=" + str(fwd[1]), LOG_DEBUG)
            except Exception as e:
                log("Forward error on " + interface.name + ": " + str(e), LOG_ERROR)

    @staticmethod
    def _ifac_validate(raw, interface):
        """Validate and strip IFAC from inbound packet. Returns raw or None."""
        has_ifac_flag = raw[0] & 0x80

        if interface is not None and interface.ifac_signing_key is not None:
            # IFAC enabled: flag MUST be set
            if not has_ifac_flag:
                log("Inbound: IFAC required but flag not set, dropping", LOG_DEBUG)
                return None

            isz = interface.ifac_size
            if len(raw) <= 2 + isz:
                log("Inbound: packet too short for IFAC, dropping", LOG_DEBUG)
                return None

            import gc
            from .crypto.hkdf import hkdf

            # Extract IFAC (not masked on wire)
            ifac = raw[2:2 + isz]

            # Generate mask
            mask = hkdf(length=len(raw), derive_from=ifac,
                         salt=interface.ifac_key)

            # Unmask (skip IFAC byte positions)
            unmasked = bytearray(len(raw))
            unmasked[0] = raw[0] ^ mask[0]
            unmasked[1] = raw[1] ^ mask[1]
            unmasked[2:2 + isz] = ifac  # IFAC not masked
            for i in range(2 + isz, len(raw)):
                unmasked[i] = raw[i] ^ mask[i]

            # Reconstruct original packet (strip IFAC, clear flag)
            new_raw = bytes([unmasked[0] & 0x7F, unmasked[1]]) + bytes(unmasked[2 + isz:])

            # Verify signature
            expected_ifac = interface.ifac_signing_key.sign(new_raw)[-isz:]
            gc.collect()

            if ifac == expected_ifac:
                log("IFAC verified " + str(len(new_raw)) + "B on " + interface.name, LOG_DEBUG)
                return new_raw
            else:
                log("Inbound: IFAC verification failed, dropping", LOG_DEBUG)
                return None
        else:
            # No IFAC on this interface: drop packets with IFAC flag
            if has_ifac_flag:
                log("Inbound: IFAC flag set but not configured, dropping", LOG_DEBUG)
                return None
            return raw

    @staticmethod
    def inbound(raw, interface=None):
        """Process an incoming raw packet from an interface"""
        from .packet import Packet
        from .identity import Identity

        try:
            if len(raw) < 2:
                return

            log("Inbound: " + str(len(raw)) + " bytes, flags=0x" + ("%02x" % raw[0]), LOG_EXTREME)

            # IFAC validation
            raw = Transport._ifac_validate(raw, interface)
            if raw is None:
                return

            packet = Packet(destination=None, data=raw)
            if not packet.unpack():
                log("Inbound: unpack failed", LOG_DEBUG)
                return

            log("Inbound: type=" + str(packet.packet_type) + " dest=" + packet.destination_hash.hex(), LOG_DEBUG)

            packet.receiving_interface = interface
            packet.hops += 1

            if hasattr(interface, 'rssi'):
                packet.rssi = interface.rssi
            if hasattr(interface, 'snr'):
                packet.snr = interface.snr

            # Check for duplicate
            packet_hash = packet.get_hash()
            if packet_hash in Transport.packet_hashlist:
                log("Inbound: duplicate packet, dropping", LOG_DEBUG)
                return

            Transport._cache_packet_hash(packet)

            # Route the packet
            local = False
            if packet.packet_type == const.PKT_ANNOUNCE:
                log("Inbound: processing announce", LOG_DEBUG)
                Transport._handle_announce(packet)
            elif packet.packet_type == const.PKT_LINKREQUEST:
                local = Transport._handle_linkrequest(packet)
            elif packet.packet_type == const.PKT_DATA:
                local = Transport._handle_data(packet)
            elif packet.packet_type == const.PKT_PROOF:
                local = Transport._handle_proof(packet)

            # Forward: announces always, other types only if not consumed locally
            if Transport.transport_enabled and interface is not None:
                if packet.packet_type == const.PKT_ANNOUNCE or not local:
                    Transport._forward(raw, interface)

        except Exception as e:
            log("Error processing inbound packet: " + str(e), LOG_ERROR)

    @staticmethod
    def sync_clock_from(unix_ts, source_hash=None):
        """Learn wall-clock time from a peer's Unix timestamp.

        Acts only on a fresh boot (clock still unset) and runs the RTC set at
        most once per power-on. Two modes:
          - Authority: if trusted_nodes is configured, one matching source sets
            the clock immediately.
          - Corroboration: with no trusted_nodes, buffer offsets from distinct
            peers and only set the clock once min_sources of them agree within
            tolerance (then apply the median). This needs no upper sanity bound.
        """
        if not Transport.time_sync_enabled or Transport._clock_synced:
            return
        # Fresh-boot only: stop once we already hold a real wall clock.
        if time.time() + _EPOCH_OFFSET >= _TIME_FLOOR:
            Transport._clock_synced = True
            return
        try:
            unix_ts = int(unix_ts)
        except (TypeError, ValueError):
            return
        # Ignore sources whose own clock is unset (and keep epoch math >= 0).
        if unix_ts < _TIME_FLOOR:
            return

        src8 = source_hash.hex()[:8] if source_hash is not None else "?"

        # Authority mode: a single trusted source is enough.
        if Transport.time_sync_trusted:
            if source_hash is not None and source_hash.hex() in Transport.time_sync_trusted:
                Transport._apply_clock(unix_ts, "trusted node " + src8)
            return

        # Corroboration mode: require agreement across distinct peers.
        if source_hash is None:
            return
        # Offset is time-invariant (true_time - our_clock), so offsets gathered
        # at different moments are directly comparable.
        offset = unix_ts - int(time.time() + _EPOCH_OFFSET)
        Transport._time_votes[source_hash.hex()] = offset
        if len(Transport._time_votes) > 16:
            Transport._time_votes.pop(next(iter(Transport._time_votes)))

        tol = Transport.time_sync_tolerance
        agree = sorted(o for o in Transport._time_votes.values() if abs(o - offset) <= tol)
        need = Transport.time_sync_min_sources
        if len(agree) >= need:
            median = agree[len(agree) // 2]
            Transport._apply_clock(int(time.time() + _EPOCH_OFFSET) + median,
                                   str(len(agree)) + " peers agree")
        else:
            log("Time sync: vote " + str(len(agree)) + "/" + str(need)
                + " from " + src8 + " (waiting for more peers)", LOG_NOTICE)

    @staticmethod
    def _apply_clock(unix_ts, detail=""):
        try:
            import machine
            local = unix_ts - _EPOCH_OFFSET   # convert Unix -> this port's epoch
            if local < 0:
                return
            t = time.gmtime(local)
            # RTC tuple: (year, month, mday, weekday, hour, minute, second, subsec)
            machine.RTC().datetime((t[0], t[1], t[2], t[6], t[3], t[4], t[5], 0))
            Transport._clock_synced = True
            Transport._time_votes = {}
            stamp = "%04d-%02d-%02d %02d:%02d:%02d UTC" % (
                t[0], t[1], t[2], t[3], t[4], t[5])
            msg = "Time synced from network: " + stamp
            if detail:
                msg += " (" + detail + ")"
            log(msg, LOG_NOTICE)
        except Exception as e:
            log("Clock sync failed: " + str(e), LOG_ERROR)

    @staticmethod
    def has_path(destination_hash):
        """True if we currently know how to reach this destination.

        Membership-only, NO expiry: a transport answers a path request by
        replaying the destination's *cached* announce, which has the same packet
        hash as the original. If reachability expired while that hash were still
        in packet_hashlist, the replayed response would be dropped as a duplicate
        and the path never re-learned. So a destination is "unreachable" only when
        we have genuinely never heard it this boot (packet_hashlist also empty),
        which avoids that dedup conflict.
        """
        return destination_hash in Transport.reachable_destinations

    @staticmethod
    def request_path(destination_hash, on_interface=None):
        """Broadcast a path request so a transport node re-announces the route.

        Wire-compatible with reference RNS: a PLAIN broadcast packet addressed to
        "rnstransport.path.request" carrying destination_hash + a random tag.
        """
        from .destination import Destination
        from .identity import Identity
        from .packet import Packet
        if Transport._path_request_dest is None:
            Transport._path_request_dest = Destination(
                None, Destination.OUT, Destination.PLAIN,
                _PATH_APP_NAME, "path", "request",
            )
        tag = Identity.get_random_hash()
        # Leaf form is dest(16)+tag(16); transport nodes insert their own id.
        if Transport.transport_enabled and Transport.identity is not None:
            data = destination_hash + Transport.identity.hash + tag
        else:
            data = destination_hash + tag
        try:
            pkt = Packet(
                Transport._path_request_dest, data, const.PKT_DATA,
                transport_type=const.TRANSPORT_BROADCAST,
                header_type=const.HDR_1,
                attached_interface=on_interface,
                create_receipt=False,
            )
            pkt.send()
            Transport._path_request_times[destination_hash] = time.time()
            log("Path request for " + destination_hash.hex()[:8], LOG_VERBOSE)
        except Exception as e:
            log("Path request failed: " + str(e), LOG_ERROR)

    @staticmethod
    def ensure_path(destination_hash, on_found, on_timeout=None,
                    timeout=_PATH_REQUEST_TIMEOUT):
        """Call on_found() as soon as a path to destination_hash is known,
        requesting one if necessary. on_timeout() fires if none appears within
        `timeout` seconds. Waiters are serviced in job_loop()."""
        if Transport.has_path(destination_hash):
            try:
                on_found()
            except Exception as e:
                log("Path on_found error: " + str(e), LOG_ERROR)
            return
        waiter = {"on_found": on_found, "on_timeout": on_timeout,
                  "deadline": time.time() + timeout}
        waiters = Transport._path_waiters.get(destination_hash)
        if waiters is None:
            Transport._path_waiters[destination_hash] = [waiter]
            Transport.request_path(destination_hash)   # first request immediately
        else:
            waiters.append(waiter)                      # request already in flight

    @staticmethod
    def _process_path_waiters():
        """job_loop tick: fire found waiters, time out stale ones, re-request."""
        if not Transport._path_waiters:
            return
        now = time.time()
        for dest in list(Transport._path_waiters.keys()):
            waiters = Transport._path_waiters[dest]
            if Transport.has_path(dest):
                del Transport._path_waiters[dest]
                Transport._path_request_times.pop(dest, None)
                for w in waiters:
                    try:
                        w["on_found"]()
                    except Exception as e:
                        log("Path on_found error: " + str(e), LOG_ERROR)
                continue
            live = []
            for w in waiters:
                if now >= w["deadline"]:
                    if w["on_timeout"] is not None:
                        try:
                            w["on_timeout"]()
                        except Exception as e:
                            log("Path on_timeout error: " + str(e), LOG_ERROR)
                else:
                    live.append(w)
            if not live:
                del Transport._path_waiters[dest]
                Transport._path_request_times.pop(dest, None)
                continue
            Transport._path_waiters[dest] = live
            last = Transport._path_request_times.get(dest, 0)
            if now - last >= _PATH_REREQUEST_INTERVAL:
                Transport.request_path(dest)

    @staticmethod
    def _handle_announce(packet):
        from .identity import Identity
        import gc; gc.collect()
        valid = Identity.validate_announce(packet)
        gc.collect()
        if valid:
            log("Valid announce from " + packet.destination_hash.hex(), LOG_NOTICE)

            # Mark the destination reachable (membership-only) so opportunistic
            # sends know a route exists and any deferred sends waiting on it can
            # fire. Covers both HDR_1 (direct) and HDR_2 (via transport) peers.
            if (len(Transport.reachable_destinations) >= const.MAX_DESTINATIONS
                    and packet.destination_hash not in Transport.reachable_destinations):
                Transport.reachable_destinations.pop(
                    next(iter(Transport.reachable_destinations)), None)
            Transport.reachable_destinations[packet.destination_hash] = time.time()

            # Bootstrap the clock from the announce timestamp (last 5 bytes of
            # the 10-byte random_hash). No-op unless time sync is enabled and
            # the clock is still unset.
            if Transport.time_sync_enabled and not Transport._clock_synced:
                try:
                    _off = const.KEYSIZE // 8 + const.NAME_HASH_LENGTH // 8
                    if packet.data is not None and len(packet.data) >= _off + 10:
                        _ts = int.from_bytes(packet.data[_off + 5:_off + 10], "big")
                        Transport.sync_clock_from(_ts, packet.destination_hash)
                except Exception:
                    pass

            # Record transport path from HDR_2 announces so outbound
            # DATA packets can be routed via the transport node. Skip our
            # own destinations — a relay echoing our announce back as HDR_2
            # would otherwise install a transport path to ourselves.
            from .destination import Destination as _Dest
            is_self = any(
                d.hash == packet.destination_hash and d.direction == _Dest.IN
                for d in Transport.destinations
            )
            if is_self:
                pass
            elif packet.header_type == const.HDR_2 and packet.transport_id:
                if len(Transport.path_table) < const.MAX_PATH_TABLE or packet.destination_hash in Transport.path_table:
                    Transport.path_table[packet.destination_hash] = packet.transport_id
                    log("Path: " + packet.destination_hash.hex()[:8] + " via transport " + packet.transport_id.hex()[:8], LOG_VERBOSE)
            elif packet.header_type == const.HDR_1:
                # Direct announce — remove transport path if any
                Transport.path_table.pop(packet.destination_hash, None)

            app_data = Identity.recall_app_data(packet.destination_hash)
            if app_data:
                log("Announce app_data: " + str(app_data), LOG_VERBOSE)
            for dest in Transport.destinations:
                if hasattr(dest, '_announce_handler') and dest._announce_handler:
                    try:
                        dest._announce_handler(
                            packet.destination_hash,
                            app_data,
                            packet,
                        )
                    except Exception as e:
                        log("Announce handler error: " + str(e), LOG_ERROR)
        else:
            log("Invalid announce for " + packet.destination_hash.hex(), LOG_DEBUG)

    @staticmethod
    def _handle_linkrequest(packet):
        from .destination import Destination
        for dest in Transport.destinations:
            # Only IN destinations can answer link requests — OUT entries
            # (peers we've heard from) live in the same table but their
            # identity has no private key.
            if dest.hash == packet.destination_hash and dest.direction == Destination.IN:
                dest.receive(packet)
                return True
        return False

    @staticmethod
    def _handle_data(packet):
        from .destination import Destination
        for dest in Transport.destinations:
            if dest.hash == packet.destination_hash and dest.direction == Destination.IN:
                import gc; gc.collect()
                if dest.receive(packet):
                    # Honour proof_strategy (mirrors reference RNS Transport.py).
                    if dest.proof_strategy == Destination.PROVE_ALL:
                        packet.prove()
                    elif dest.proof_strategy == Destination.PROVE_APP:
                        if dest.proof_requested_callback is not None:
                            try:
                                if dest.proof_requested_callback(packet):
                                    packet.prove()
                            except Exception as e:
                                log("Proof requested callback error: " + str(e), LOG_ERROR)
                return True
        # Check active links
        for link in Transport.active_links:
            if link.link_id == packet.destination_hash:
                link.receive(packet)
                return True
        return False

    @staticmethod
    def _handle_proof(packet):
        if packet.context == const.CTX_LRPROOF:
            # Link request proof
            for link in Transport.pending_links:
                if link.link_id == packet.destination_hash:
                    link.validate_proof(packet)
                    return True
        elif packet.context == const.CTX_RESOURCE_PRF:
            # Resource proof — route to the link
            for link in Transport.active_links:
                if link.link_id == packet.destination_hash:
                    link._handle_resource_prf(packet.data)
                    return True
        else:
            # Regular proof - check receipts
            for receipt in Transport.receipts:
                if receipt.validate_proof_packet(packet):
                    return True
        return False

    @staticmethod
    def hops_to(destination_hash):
        """Return known hop count to destination, or 0 if unknown"""
        if destination_hash in Transport.destination_table:
            return Transport.destination_table[destination_hash].get("hops", 0)
        return 0

    @staticmethod
    async def job_loop():
        """Main transport maintenance loop - run as async task"""
        while Transport._jobs_running:
            try:
                now = time.time()

                # Check receipt timeouts
                timed_out = []
                for receipt in Transport.receipts:
                    receipt.check_timeout()
                    if receipt.status != 1:  # SENT
                        timed_out.append(receipt)
                for r in timed_out:
                    if r in Transport.receipts:
                        Transport.receipts.remove(r)

                # Check pending link timeouts
                expired_links = []
                for link in Transport.pending_links:
                    if hasattr(link, 'check_timeout'):
                        link.check_timeout()
                        if link.status == 0x02:  # CLOSED
                            expired_links.append(link)
                for l in expired_links:
                    if l in Transport.pending_links:
                        Transport.pending_links.remove(l)

                # Check active link keepalives and stale cleanup
                closed_links = []
                for link in Transport.active_links:
                    link.check_keepalive()
                    if link.status == 0x02:  # CLOSED
                        closed_links.append(link)
                for l in closed_links:
                    if l in Transport.active_links:
                        Transport.active_links.remove(l)

                # Service deferred sends waiting on a path request.
                Transport._process_path_waiters()

                import gc
                gc.collect()

            except Exception as e:
                log("Transport job error: " + str(e), LOG_ERROR)

            import uasyncio as asyncio
            await asyncio.sleep(0.25)
