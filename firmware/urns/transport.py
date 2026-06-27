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
    path_table = {}          # dest_hash -> transport_id (Phase 0); rich entry in Phase 1
    blackholed_identities = []

    # Directed-routing tables (relay/transport mode). See firmware transport plan.
    reverse_table = {}          # truncated_pkt_hash -> [recv_if, outbound_if, ts]
    link_table = {}             # link_id -> [ts, next_hop, nh_if, rem_hops, recv_if, hops, dest, validated, proof_tmo]
    packet_cache = {}           # announce_hash -> raw announce bytes (for path-request answers)
    path_states = {}            # dest_hash -> reachability state
    control_destinations = []   # IN PLAIN control dests (e.g. rnstransport.path.request)
    control_hashes = []         # their hashes (special-cased in admission/forwarding)
    discovery_path_requests = {} # dest_hash -> {"requesting_interface", "timeout"}
    _pr_tags = []                # recent (dest_hash + tag) path-request dedup tags

    # Validate link-request proofs before relaying them (anti-DoS on open mesh).
    # Native-gated: skipped when native Ed25519 is unavailable (avoids ~2s verify).
    strict_lr_validation = True

    # Maintenance / persistence (Phase 5/6)
    _last_cull = 0
    _last_persist = 0
    _announce_rate = {}          # dest_hash -> [recent announce timestamps]
    persist_path = None          # set by Reticulum to enable path-table flash persistence

    # Relay activity counters — surfaced in the transport-router status line and
    # in the NOTICE-level "Relay ..." log lines (so a router shows its forwarding).
    relayed_announces = 0
    relayed_data = 0
    relayed_links = 0
    relayed_proofs = 0

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
        Transport._register_control_destinations()
        if Transport.transport_enabled:
            log("Transport engine started — TRANSPORT MODE", LOG_NOTICE)
        else:
            log("Transport engine started", LOG_VERBOSE)

    @staticmethod
    def _register_control_destinations():
        """Register the IN PLAIN rnstransport.path.request destination so this
        node can ANSWER path requests (the OUT send side lives in request_path).
        Without this the node can ask for paths but never reply to them."""
        try:
            from .destination import Destination
            d = Destination(None, Destination.IN, Destination.PLAIN,
                            _PATH_APP_NAME, "path", "request")
            d.set_packet_callback(Transport.path_request_handler)
            d.set_proof_strategy(Destination.PROVE_NONE)
            Transport.control_destinations.append(d)
            if d.hash not in Transport.control_hashes:
                Transport.control_hashes.append(d.hash)
            log("Registered path-request control destination " + d.hash.hex()[:8], LOG_VERBOSE)
        except Exception as e:
            log("Control destination registration failed: " + str(e), LOG_ERROR)

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

            # Directed egress: when we know a multi-hop transport path to the
            # destination, transmit ONLY on that path's interface instead of
            # broadcasting to all. Announces and directly-reachable (hops<=1)
            # destinations still broadcast.
            _path = None
            if (packet.attached_interface is None
                    and getattr(packet, "destination_hash", None) is not None
                    and packet.packet_type != const.PKT_ANNOUNCE):
                _path = Transport.path_table.get(packet.destination_hash)

            for interface in Transport.interfaces:
                if interface.online:
                    if packet.attached_interface is not None and interface is not packet.attached_interface:
                        continue
                    if (_path is not None and _path[const.IDX_PT_HOPS] > 1
                            and interface is not _path[const.IDX_PT_RECV_IF]):
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
        if packet_hash in Transport.packet_hashlist:
            return
        if len(Transport.packet_hashlist) >= const.MAX_PACKET_HASHLIST:
            Transport.packet_hashlist.pop(0)
        Transport.packet_hashlist.append(packet_hash)

    @staticmethod
    def transmit(interface, raw):
        """Directed send: transmit a raw packet out ONE specific interface
        (used by relay forwarding instead of the broadcast-to-all outbound()).
        Returns True on success. Honours online + OUT gating. The egress
        interface re-applies its own framing + IFAC inside process_outgoing."""
        if interface is None or not interface.online:
            return False
        if not getattr(interface, "OUT", True):
            return False
        try:
            result = interface.process_outgoing(raw)
            return result or result is None
        except Exception as e:
            log("transmit error on " + str(interface) + ": " + str(e), LOG_ERROR)
            return False

    @staticmethod
    def cache_announce(announce_hash, raw):
        """Cache an announce's raw bytes (keyed by its route-independent hash)
        so this node can answer path requests by replaying it. Bounded LRU."""
        if announce_hash in Transport.packet_cache:
            return
        if len(Transport.packet_cache) >= const.MAX_PACKET_CACHE:
            Transport.packet_cache.pop(next(iter(Transport.packet_cache)), None)
        Transport.packet_cache[announce_hash] = bytes(raw)

    @staticmethod
    def get_cached(announce_hash):
        return Transport.packet_cache.get(announce_hash)

    @staticmethod
    def packet_filter(packet):
        """Admission gate (ported from reference RNS Transport.packet_filter).
        Returns True to process the packet, False to drop. This is the routing
        correctness spine: drop transit traffic for other relays, let link/
        resource sub-packets bypass dedup, cap PLAIN/GROUP to one hop, and
        deduplicate by route-independent hash (allowing SINGLE re-announces)."""
        # Drop non-announce packets addressed to a different transport instance.
        if packet.transport_id is not None and packet.packet_type != const.PKT_ANNOUNCE:
            if Transport.identity is None or packet.transport_id != Transport.identity.hash:
                return False

        # Link/resource sub-packets legitimately repeat — never hash-filter them.
        if packet.context in (const.CTX_KEEPALIVE, const.CTX_RESOURCE,
                              const.CTX_RESOURCE_REQ, const.CTX_RESOURCE_PRF,
                              const.CTX_CACHE_REQUEST, const.CTX_CHANNEL):
            return True

        # PLAIN / GROUP destinations are single-hop only (path requests, etc.).
        if packet.destination_type in (const.DEST_PLAIN, const.DEST_GROUP):
            if packet.packet_type == const.PKT_ANNOUNCE:
                return False                       # PLAIN/GROUP announces are invalid
            return packet.hops <= 1

        # Deduplicate by route-independent hash.
        if packet.packet_hash not in Transport.packet_hashlist:
            return True
        # Already seen: let SINGLE announces through so re-announces and replayed
        # cached announces can refresh paths (should_add decides the no-op).
        if (packet.packet_type == const.PKT_ANNOUNCE
                and packet.destination_type == const.DEST_SINGLE):
            return True
        return False

    @staticmethod
    def _should_remember(packet):
        """Whether to add this packet's hash to the dedup list NOW. Link/resource
        sub-packets repeat by design and are never remembered. Link-table transit
        packets and link-request proofs are DEFERRED — adding their hash before we
        know it's ours to relay could drop a packet we must forward (matching
        reference RNS inbound 1498-1503). They are remembered when forwarded."""
        if packet.context in (const.CTX_KEEPALIVE, const.CTX_RESOURCE,
                              const.CTX_RESOURCE_REQ, const.CTX_RESOURCE_PRF,
                              const.CTX_CACHE_REQUEST, const.CTX_CHANNEL):
            return False
        if packet.destination_hash in Transport.link_table:
            return False
        if (packet.packet_type == const.PKT_PROOF
                and packet.context == const.CTX_LRPROOF):
            return False
        return True

    @staticmethod
    def _announce_emitted(packet):
        """Emission timebase of an announce: the last 5 bytes of its 10-byte
        random_hash (big-endian seconds). Used for freshness/replay rejection."""
        try:
            off = const.KEYSIZE // 8 + const.NAME_HASH_LENGTH // 8
            if packet.data is not None and len(packet.data) >= off + 10:
                return int.from_bytes(packet.data[off + 5:off + 10], "big")
        except Exception:
            pass
        return 0

    @staticmethod
    def _announce_airtime_ok(interface, pkt_len):
        """Per-interface announce airtime cap (token bucket). Limits announce TX
        to ANNOUNCE_CAP of the link's airtime so a busy mesh does not saturate
        slow media (LoRa). Excess is dropped (re-announced later), not queued —
        a deliberate MCU simplification of reference RNS's announce queue."""
        bitrate = getattr(interface, "bitrate", 0) or 0
        if bitrate <= 0:
            return True
        now = time.time()
        allowed_at = getattr(interface, "_announce_allowed_at", 0)
        if now < allowed_at:
            return False
        tx_time = (pkt_len * 8) / bitrate
        interface._announce_allowed_at = now + (tx_time / const.ANNOUNCE_CAP)
        return True

    @staticmethod
    def _enqueue_announce(packet, received_from):
        """Queue a validated announce for timed rebroadcast (transport mode)."""
        dest = packet.destination_hash
        now = time.time()
        jitter = (os.urandom(1)[0] / 255.0) * const.PATHFINDER_RW
        if (len(Transport.announce_table) >= const.MAX_ANNOUNCE_TABLE
                and dest not in Transport.announce_table):
            oldest = min(Transport.announce_table,
                         key=lambda k: Transport.announce_table[k][const.IDX_AT_TIMESTAMP])
            Transport.announce_table.pop(oldest, None)
        # [ts, retransmit_tmo, retries, recv_from, hops, raw, lcl_rbrd, blk_rbrd, attchd_if]
        Transport.announce_table[dest] = [now, now + jitter, 0, received_from,
                                          packet.hops, packet.raw, 0, False, None]

    @staticmethod
    def _service_announce_table():
        """job_loop tick: rebroadcast queued announces after their jittered
        delay, with retries + neighbour suppression (LOCAL_REBROADCASTS_MAX)."""
        now = time.time()
        completed = []
        for dest in list(Transport.announce_table.keys()):
            entry = Transport.announce_table[dest]
            retries = entry[const.IDX_AT_RETRIES]
            if (retries >= const.LOCAL_REBROADCASTS_MAX
                    or retries > const.PATHFINDER_R):
                completed.append(dest)
                continue
            if now > entry[const.IDX_AT_RTMO]:
                entry[const.IDX_AT_RTMO] = now + const.PATHFINDER_G + const.PATHFINDER_RW
                entry[const.IDX_AT_RETRIES] += 1
                try:
                    Transport._rebroadcast_announce(entry)
                except Exception as e:
                    log("Announce rebroadcast error: " + str(e), LOG_ERROR)
        for d in completed:
            Transport.announce_table.pop(d, None)

    @staticmethod
    def _rebroadcast_announce(entry):
        """Re-emit a stored announce as HDR_2 with OUR transport_id, on every
        online OUT interface (subject to the airtime cap). hops carries the
        stored (already-incremented) value; the next node increments again.
        Dedup (route-independent hash) + should_add prevent loops/bad paths."""
        if Transport.identity is None:
            return
        raw = entry[const.IDX_AT_RAW]
        hops = entry[const.IDX_AT_HOPS]
        attached_if = entry[const.IDX_AT_ATTCHD_IF]
        flags = raw[0]
        tid = Transport.identity.hash
        if (flags & 0x40) == 0x00:
            # HDR_1 -> HDR_2: set header(bit6) + TRANSPORT(bit4), insert transport_id
            new_raw = bytes([flags | 0x50, hops]) + tid + raw[2:]
        else:
            # Already HDR_2: replace transport_id, set hops
            new_raw = bytes([flags, hops]) + tid + raw[18:]
        # Path responses carry CTX_PATH_RESPONSE (context byte at offset 34 of the
        # HDR_2 form) so receivers treat them as a path reply, not a fresh announce.
        if entry[const.IDX_AT_BLK_RBRD] and len(new_raw) > 34:
            new_raw = new_raw[:34] + bytes([const.CTX_PATH_RESPONSE]) + new_raw[35:]
        sent_on = []
        for interface in Transport.interfaces:
            if not interface.online or not getattr(interface, "OUT", True):
                continue
            if attached_if is not None and interface is not attached_if:
                continue
            if not Transport._announce_airtime_ok(interface, len(new_raw)):
                log("Announce airtime cap hit on " + str(interface), LOG_DEBUG)
                continue
            if Transport.transmit(interface, new_raw):
                sent_on.append(interface.name)
        if sent_on:
            Transport.relayed_announces += 1
            _d8 = new_raw[18:34].hex()[:8] if len(new_raw) >= 34 else "?"
            log("Relay announce " + _d8 + " hops=" + str(hops)
                + " -> " + ",".join(sent_on), LOG_NOTICE)

    @staticmethod
    def _add_reverse(trunc_hash, recv_if, out_if):
        """Record the reverse path for a forwarded DATA packet so its returning
        proof can be routed back. Keyed by the packet's truncated hash."""
        if (len(Transport.reverse_table) >= const.MAX_REVERSE_TABLE
                and trunc_hash not in Transport.reverse_table):
            oldest = min(Transport.reverse_table,
                         key=lambda k: Transport.reverse_table[k][const.IDX_RT_TIMESTAMP])
            Transport.reverse_table.pop(oldest, None)
        Transport.reverse_table[trunc_hash] = [recv_if, out_if, time.time()]

    @staticmethod
    def _transit_forward(packet):
        """If we are the designated next hop for a DATA or LINKREQUEST packet in
        transport, forward it toward the destination. DATA records a reverse-path
        entry (for the returning proof); LINKREQUEST clamps the link MTU to the
        egress interface and records a link-table entry (for in-link traffic).
        Returns True if the relay consumed the packet (forwarded or dropped)."""
        if packet.transport_id is None or Transport.identity is None:
            return False
        if packet.transport_id != Transport.identity.hash:
            return False
        if packet.packet_type not in (const.PKT_DATA, const.PKT_LINKREQUEST):
            return False

        dest = packet.destination_hash
        if dest in Transport.blackholed_identities:
            return True   # blackholed — consumed (dropped)

        # Self-destination guard: if dest is one of our own IN destinations the
        # packet terminates here — let local delivery handle it.
        from .destination import Destination as _Dest
        for d in Transport.destinations:
            if d.hash == dest and d.direction == _Dest.IN:
                return False

        entry = Transport.path_table.get(dest)
        if entry is None:
            log("Transit: no path to " + dest.hex()[:8] + ", dropping", LOG_DEBUG)
            return True   # consumed (dropped) — do not deliver locally

        next_hop = entry[const.IDX_PT_NEXT_HOP]
        remaining = entry[const.IDX_PT_HOPS]
        out_if = entry[const.IDX_PT_RECV_IF]
        raw = packet.raw
        hops = packet.hops   # already incremented in inbound()
        _tid_end = 2 + const.TRUNCATED_HASHLENGTH // 8   # = 18 (end of transport_id)

        if remaining > 1:
            # Keep HDR_2; swap transport_id -> next_hop; set hops.
            new_raw = raw[0:1] + bytes([hops]) + next_hop + raw[_tid_end:]
        elif remaining == 1:
            # Last hop: strip transport headers to HDR_1 BROADCAST for direct delivery.
            new_flags = (const.HDR_1 << 6) | (const.TRANSPORT_BROADCAST << 4) | (raw[0] & 0x0F)
            new_raw = bytes([new_flags, hops]) + raw[_tid_end:]
        else:  # remaining == 0 (degenerate; kept for parity with reference RNS)
            new_raw = raw[0:1] + bytes([hops]) + raw[2:]

        if packet.packet_type == const.PKT_LINKREQUEST:
            # Clamp the negotiated link MTU to what the next-hop interface can
            # carry, then record a link-table entry so in-link traffic + the
            # LRPROOF can be routed. link_id is signalling-independent.
            new_raw = Transport._clamp_link_mtu(new_raw, out_if, packet)
            try:
                link_id = Transport._link_id_from_lr(packet)
                Transport._add_link(link_id, next_hop, out_if, remaining,
                                    packet.receiving_interface, packet.hops, dest)
                log("Transit LINKREQUEST " + dest.hex()[:8]
                    + " link_id=" + link_id.hex()[:8], LOG_DEBUG)
            except Exception as e:
                log("Link transit setup error: " + str(e), LOG_ERROR)
        else:
            # Record the reverse path so the returning proof can be routed back.
            Transport._add_reverse(packet.getTruncatedHash(),
                                   packet.receiving_interface, out_if)

        Transport.transmit(out_if, new_raw)
        entry[const.IDX_PT_TIMESTAMP] = time.time()
        if packet.packet_type == const.PKT_LINKREQUEST:
            Transport.relayed_links += 1
            _kind = "LINKREQ"
        else:
            Transport.relayed_data += 1
            _kind = "DATA"
        log("Relay " + _kind + " " + dest.hex()[:8] + " -> " + next_hop.hex()[:8]
            + " on " + str(out_if) + " (rem " + str(remaining) + ")", LOG_NOTICE)
        return True

    @staticmethod
    def _link_id_from_lr(packet):
        """Derive link_id from a LINKREQUEST packet — the hash of its hashable
        part with the trailing MTU signalling bytes stripped (matches link.py /
        reference RNS Link.link_id_from_lr_packet), so it is route- and
        clamp-independent: every node agrees on the same link_id."""
        from .identity import Identity
        from .link import ECPUBSIZE
        hashable = packet.get_hashable_part()
        if packet.data is not None and len(packet.data) > ECPUBSIZE:
            hashable = hashable[:-(len(packet.data) - ECPUBSIZE)]
        return Identity.full_hash(hashable)[:const.TRUNCATED_HASHLENGTH // 8]

    @staticmethod
    def _clamp_link_mtu(new_raw, out_if, packet):
        """Rewrite the LINKREQUEST's 3 trailing MTU signalling bytes down to the
        next-hop interface's HW_MTU, so a link across a LoRa/WiFi boundary cannot
        negotiate an MTU the bottleneck hop can't carry (silent link stalls)."""
        from .link import ECPUBSIZE, LINK_MTU_SIZE, _parse_signalling, _signalling_bytes
        if packet.data is None or len(packet.data) <= ECPUBSIZE:
            return new_raw   # no signalling present
        if len(new_raw) < LINK_MTU_SIZE:
            return new_raw
        try:
            peer_mtu, mode = _parse_signalling(new_raw[-LINK_MTU_SIZE:])
        except Exception:
            return new_raw
        nh_mtu = getattr(out_if, "HW_MTU", const.MTU)
        if peer_mtu > 0 and nh_mtu < peer_mtu:
            new_raw = new_raw[:-LINK_MTU_SIZE] + _signalling_bytes(nh_mtu, mode)
            log("Clamped link MTU " + str(peer_mtu) + " -> " + str(nh_mtu)
                + " for " + str(out_if), LOG_DEBUG)
        return new_raw

    @staticmethod
    def _add_link(link_id, next_hop, nh_if, rem_hops, recv_if, taken_hops, dest):
        """Record a transit link-table entry (evict oldest if full). Proof timeout
        is hop-scaled (DEFAULT_PER_HOP_TIMEOUT * remaining hops)."""
        now = time.time()
        proof_tmo = now + const.DEFAULT_PER_HOP_TIMEOUT * max(1, rem_hops)
        if (len(Transport.link_table) >= const.MAX_LINK_TABLE
                and link_id not in Transport.link_table):
            oldest = min(Transport.link_table,
                         key=lambda k: Transport.link_table[k][const.IDX_LT_TIMESTAMP])
            Transport.link_table.pop(oldest, None)
        # [ts, next_hop, nh_if, rem_hops, recv_if, hops, dest, validated, proof_tmo]
        Transport.link_table[link_id] = [now, next_hop, nh_if, rem_hops, recv_if,
                                         taken_hops, dest, False, proof_tmo]

    @staticmethod
    def _transit_link(packet):
        """Route an in-link packet (dest == link_id) through this relay, choosing
        the egress interface from the link-table entry + hop-count direction
        checks. Returns True if consumed (forwarded)."""
        if packet.packet_type in (const.PKT_ANNOUNCE, const.PKT_LINKREQUEST):
            return False
        if packet.context == const.CTX_LRPROOF:
            return False   # link-request proofs handled in _handle_proof
        entry = Transport.link_table.get(packet.destination_hash)
        if entry is None:
            return False
        nh_if = entry[const.IDX_LT_NH_IF]
        recv_if = entry[const.IDX_LT_RECV_IF]
        out_if = None
        if nh_if is recv_if:
            # Same interface both directions (shared medium) — repeat if the hop
            # count matches one of the expected values.
            if (packet.hops == entry[const.IDX_LT_REM_HOPS]
                    or packet.hops == entry[const.IDX_LT_HOPS]):
                out_if = nh_if
        else:
            # Different interfaces — transmit on the opposite one, validating the
            # expected hop count for that direction.
            if packet.receiving_interface is nh_if:
                if packet.hops == entry[const.IDX_LT_REM_HOPS]:
                    out_if = recv_if
            elif packet.receiving_interface is recv_if:
                if packet.hops == entry[const.IDX_LT_HOPS]:
                    out_if = nh_if
        if out_if is None:
            return False   # not our turn / hop mismatch
        Transport._cache_packet_hash(packet)   # NOW remember — it's ours to relay
        new_raw = packet.raw[0:1] + bytes([packet.hops]) + packet.raw[2:]
        Transport.transmit(out_if, new_raw)
        entry[const.IDX_LT_TIMESTAMP] = time.time()
        Transport.relayed_links += 1
        log("Relay link " + packet.destination_hash.hex()[:8]
            + " -> " + str(out_if), LOG_NOTICE)
        return True

    @staticmethod
    def _validate_transit_lr_proof(packet, entry):
        """Verify a link-request proof's Ed25519 signature before relaying it
        (anti-DoS on an open mesh). Native-gated: returns True (forward) when
        native crypto is unavailable, to avoid a ~2s blocking verify per link."""
        if not Transport.strict_lr_validation:
            return True
        try:
            from .crypto import ed25519
            native = ed25519.have_native()
        except Exception:
            native = False
        if not native:
            return True
        try:
            from .identity import Identity
            from .link import ECPUBSIZE, LINK_MTU_SIZE, _parse_signalling, _signalling_bytes
            SIG = const.SIGLENGTH // 8
            half = ECPUBSIZE // 2
            data = packet.data
            if data is None or len(data) not in (SIG + half, SIG + half + LINK_MTU_SIZE):
                return False
            signalling = b""
            if len(data) == SIG + half + LINK_MTU_SIZE:
                mtu, mode = _parse_signalling(data[SIG + half:])
                signalling = _signalling_bytes(mtu, mode)
            peer_pub = data[SIG:SIG + half]
            peer_identity = Identity.recall(entry[const.IDX_LT_DEST])
            if peer_identity is None:
                return False
            peer_sig_pub = peer_identity.get_public_key()[half:ECPUBSIZE]
            signed = packet.destination_hash + peer_pub + peer_sig_pub + signalling
            return bool(peer_identity.validate(data[:SIG], signed))
        except Exception as e:
            log("LRPROOF validation error: " + str(e), LOG_DEBUG)
            return False

    @staticmethod
    def _transit_lr_proof(packet):
        """Relay a link-request proof back toward the link initiator. Validates
        the proof (native-gated), marks the link validated, forwards via the
        interface the LINKREQUEST arrived on. Returns True if consumed."""
        entry = Transport.link_table.get(packet.destination_hash)
        if entry is None:
            return False
        if packet.hops != entry[const.IDX_LT_REM_HOPS]:
            log("Transit LRPROOF hop mismatch, ignoring", LOG_DEBUG)
            return False
        if packet.receiving_interface is not entry[const.IDX_LT_NH_IF]:
            log("Transit LRPROOF on wrong interface, ignoring", LOG_DEBUG)
            return False
        if not Transport._validate_transit_lr_proof(packet, entry):
            log("Transit LRPROOF failed validation, dropping", LOG_DEBUG)
            return True   # consumed (dropped)
        Transport._cache_packet_hash(packet)
        entry[const.IDX_LT_VALIDATED] = True
        new_raw = packet.raw[0:1] + bytes([packet.hops]) + packet.raw[2:]
        Transport.transmit(entry[const.IDX_LT_RECV_IF], new_raw)
        Transport.relayed_proofs += 1
        log("Relay LRPROOF " + packet.destination_hash.hex()[:8]
            + " -> " + str(entry[const.IDX_LT_RECV_IF]) + " (validated)", LOG_NOTICE)
        return True

    @staticmethod
    def _transit_proof(packet):
        """If this proof corresponds to a DATA packet we forwarded, route it
        back along the reverse path. Returns True if forwarded."""
        rkey = packet.destination_hash
        entry = Transport.reverse_table.get(rkey)
        if entry is None:
            return False
        # The proof must arrive on the interface we forwarded the data toward.
        if packet.receiving_interface is not entry[const.IDX_RT_OUTB_IF]:
            log("Transit proof on wrong interface, not forwarding", LOG_DEBUG)
            return False
        Transport.reverse_table.pop(rkey, None)
        new_raw = packet.raw[0:1] + bytes([packet.hops]) + packet.raw[2:]
        Transport.transmit(entry[const.IDX_RT_RECV_IF], new_raw)
        Transport.relayed_proofs += 1
        log("Relay proof " + rkey.hex()[:8] + " -> "
            + str(entry[const.IDX_RT_RECV_IF]), LOG_NOTICE)
        return True

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
        """Process an incoming raw packet from an interface."""
        from .packet import Packet

        try:
            if len(raw) < 2:
                return

            log("Inbound: " + str(len(raw)) + " bytes, flags=0x" + ("%02x" % raw[0]), LOG_EXTREME)

            # IFAC validation (strips access-code, returns clean raw or None)
            raw = Transport._ifac_validate(raw, interface)
            if raw is None:
                return

            if Transport.identity is None:
                return

            packet = Packet(destination=None, data=raw)
            if not packet.unpack():
                log("Inbound: unpack failed", LOG_DEBUG)
                return

            packet.receiving_interface = interface
            packet.hops += 1

            if interface is not None:
                if hasattr(interface, 'rssi'):
                    packet.rssi = interface.rssi
                if hasattr(interface, 'snr'):
                    packet.snr = interface.snr

            # Admission gate: dedup, drop traffic for other relays, PLAIN/GROUP
            # hop cap, link/resource context whitelist.
            if not Transport.packet_filter(packet):
                log("Inbound: filtered/duplicate, dropping", LOG_EXTREME)
                return

            if Transport._should_remember(packet):
                Transport._cache_packet_hash(packet)

            log("Inbound: type=" + str(packet.packet_type) + " dest=" + packet.destination_hash.hex(), LOG_DEBUG)

            # Directed transit: are we the next hop for a DATA/LINKREQUEST in
            # transport, or an in-link packet we relay? (Consumes the packet so
            # it isn't also delivered locally. Proofs route in _handle_proof.)
            if Transport._transit_forward(packet):
                return
            if Transport._transit_link(packet):
                return

            # Route by type (local delivery; transit proofs handled in _handle_proof).
            if packet.packet_type == const.PKT_ANNOUNCE:
                Transport._handle_announce(packet)
            elif packet.packet_type == const.PKT_LINKREQUEST:
                Transport._handle_linkrequest(packet)
            elif packet.packet_type == const.PKT_DATA:
                Transport._handle_data(packet)
            elif packet.packet_type == const.PKT_PROOF:
                Transport._handle_proof(packet)

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
    def path_request_handler(data, packet):
        """Inbound callback on the rnstransport.path.request control destination.
        Parses a path request and answers it (or recursively discovers the path).
        Wire format: dest_hash(16) [+ requester_transport_id(16)] + tag."""
        try:
            hlen = const.TRUNCATED_HASHLENGTH // 8
            if data is None or len(data) < hlen:
                return
            dest_hash = data[:hlen]
            requester_tid = None
            if len(data) > 2 * hlen:
                requester_tid = data[hlen:2 * hlen]
                tag = data[2 * hlen:]
            elif len(data) > hlen:
                tag = data[hlen:]
            else:
                tag = b""
            if not tag:
                return                       # tagless requests are ignored
            tag = tag[:hlen]
            unique = dest_hash + tag
            if unique in Transport._pr_tags:
                return                       # already handled this exact request
            Transport._pr_tags.append(unique)
            if len(Transport._pr_tags) > 32:
                Transport._pr_tags.pop(0)
            recv_if = getattr(packet, "receiving_interface", None)
            Transport._answer_path_request(dest_hash, recv_if, requester_tid, tag)
        except Exception as e:
            log("path_request_handler error: " + str(e), LOG_ERROR)

    @staticmethod
    def _answer_path_request(dest_hash, attached_interface, requester_tid, tag):
        from .destination import Destination as _Dest
        # 1) Dest is one of our own destinations -> announce it directly.
        for d in Transport.destinations:
            if (d.hash == dest_hash and d.direction == _Dest.IN
                    and d.type == _Dest.SINGLE):
                try:
                    d.announce(path_response=True, attached_interface=attached_interface)
                    log("Path request: answered local dest " + dest_hash.hex()[:8], LOG_VERBOSE)
                except Exception as e:
                    log("Local path answer error: " + str(e), LOG_ERROR)
                return
        # 2) Transport node with a cached announce -> replay it as a PATH_RESPONSE.
        entry = Transport.path_table.get(dest_hash)
        if Transport.transport_enabled and entry is not None:
            # Don't answer the node we would route through.
            if requester_tid is not None and requester_tid == entry[const.IDX_PT_NEXT_HOP]:
                return
            raw = Transport.get_cached(entry[const.IDX_PT_ANNOUNCE])
            if raw is not None:
                Transport._enqueue_path_response(dest_hash, raw, entry, attached_interface)
                log("Path request: answered " + dest_hash.hex()[:8] + " from cache", LOG_VERBOSE)
                return
        # 3) Unknown path -> recursively discover it (re-originate on other ifaces).
        if Transport.transport_enabled:
            Transport._recursive_path_discovery(dest_hash, attached_interface, tag)

    @staticmethod
    def _enqueue_path_response(dest_hash, raw, path_entry, attached_interface):
        """Queue a cached announce for immediate one-shot re-emission as a
        PATH_RESPONSE, targeted at the requesting interface only."""
        now = time.time()
        # retransmit_tmo=0 -> fires on the next job tick; retries=PATHFINDER_R so
        # it is emitted exactly once. blk_rbrd=True -> PATH_RESPONSE context.
        # [ts, retransmit_tmo, retries, recv_from, hops, raw, lcl_rbrd, blk_rbrd, attchd_if]
        Transport.announce_table[dest_hash] = [
            now, 0, const.PATHFINDER_R, path_entry[const.IDX_PT_NEXT_HOP],
            path_entry[const.IDX_PT_HOPS], raw, 0, True, attached_interface,
        ]

    @staticmethod
    def _recursive_path_discovery(dest_hash, attached_interface, tag):
        """We don't know the path: re-originate a path request on every other
        interface and remember the requester so we can answer when the announce
        arrives (serviced in _handle_announce)."""
        if dest_hash in Transport.discovery_path_requests:
            return
        Transport.discovery_path_requests[dest_hash] = {
            "requesting_interface": attached_interface,
            "timeout": time.time() + _PATH_REQUEST_TIMEOUT,
        }
        for iface in Transport.interfaces:
            if iface is attached_interface or not iface.online:
                continue
            try:
                Transport.request_path(dest_hash, on_interface=iface)
            except Exception as e:
                log("Recursive request_path error: " + str(e), LOG_DEBUG)
        log("Path request: recursive discovery for " + dest_hash.hex()[:8], LOG_VERBOSE)

    # ------------------------------------------------------------------
    # Maintenance, flood-control, failover, persistence (Phase 5/6)
    # ------------------------------------------------------------------
    @staticmethod
    def _cull_tables():
        """Periodic maintenance: expire stale routing entries, purge entries
        whose interface went offline (the WiFi-flap case), time out pending
        path discoveries, and drop unreferenced cached announces."""
        now = time.time()
        online = set(i for i in Transport.interfaces if i.online)

        def _if_ok(iface):
            return iface is None or iface in online

        # path_table: expiry + offline-interface purge
        for dest in list(Transport.path_table.keys()):
            e = Transport.path_table[dest]
            if now >= e[const.IDX_PT_EXPIRES] or not _if_ok(e[const.IDX_PT_RECV_IF]):
                Transport.path_table.pop(dest, None)
                Transport.reachable_destinations.pop(dest, None)
                Transport.path_states.pop(dest, None)

        # reverse_table: timeout + offline purge
        for k in list(Transport.reverse_table.keys()):
            e = Transport.reverse_table[k]
            if (now >= e[const.IDX_RT_TIMESTAMP] + const.REVERSE_TIMEOUT
                    or not _if_ok(e[const.IDX_RT_RECV_IF])
                    or not _if_ok(e[const.IDX_RT_OUTB_IF])):
                Transport.reverse_table.pop(k, None)

        # link_table: timeout + offline purge
        for k in list(Transport.link_table.keys()):
            e = Transport.link_table[k]
            if (now >= e[const.IDX_LT_TIMESTAMP] + const.LINK_ENTRY_TIMEOUT
                    or not _if_ok(e[const.IDX_LT_NH_IF])
                    or not _if_ok(e[const.IDX_LT_RECV_IF])):
                Transport.link_table.pop(k, None)

        # discovery_path_requests: timeout
        for dest in list(Transport.discovery_path_requests.keys()):
            if now >= Transport.discovery_path_requests[dest].get("timeout", 0):
                Transport.discovery_path_requests.pop(dest, None)

        # _announce_rate: drop empty/expired windows
        for dest in list(Transport._announce_rate.keys()):
            times = Transport._announce_rate[dest]
            while times and times[0] < now - const.ANNOUNCE_RATE_WINDOW:
                times.pop(0)
            if not times:
                Transport._announce_rate.pop(dest, None)

        # packet_cache: drop announces no longer referenced by any live path
        if Transport.packet_cache:
            referenced = set(Transport.path_table[d][const.IDX_PT_ANNOUNCE]
                             for d in Transport.path_table)
            for h in list(Transport.packet_cache.keys()):
                if h not in referenced:
                    Transport.packet_cache.pop(h, None)

    @staticmethod
    def _announce_rate_ok(dest_hash):
        """Per-source announce rate-limit. Returns False (throttle the rebroadcast,
        but still keep the path) once a source exceeds ANNOUNCE_RATE_MAX announces
        within ANNOUNCE_RATE_WINDOW seconds."""
        now = time.time()
        times = Transport._announce_rate.get(dest_hash)
        if times is None:
            times = []
            Transport._announce_rate[dest_hash] = times
        cutoff = now - const.ANNOUNCE_RATE_WINDOW
        while times and times[0] < cutoff:
            times.pop(0)
        if len(times) >= const.ANNOUNCE_RATE_MAX:
            log("Announce rate limit for " + dest_hash.hex()[:8] + ", throttling", LOG_DEBUG)
            return False
        times.append(now)
        return True

    @staticmethod
    def blackhole(dest_hash):
        """Drop all announces from / transit packets to a destination hash."""
        if dest_hash not in Transport.blackholed_identities:
            Transport.blackholed_identities.append(dest_hash)
            Transport.path_table.pop(dest_hash, None)
            Transport.reachable_destinations.pop(dest_hash, None)
            log("Blackholed " + dest_hash.hex()[:8], LOG_NOTICE)

    @staticmethod
    def unblackhole(dest_hash):
        if dest_hash in Transport.blackholed_identities:
            Transport.blackholed_identities.remove(dest_hash)
            log("Un-blackholed " + dest_hash.hex()[:8], LOG_NOTICE)

    @staticmethod
    def expire_path(dest_hash):
        """Forget the path to a destination (e.g. after it proved unresponsive),
        so the next send triggers a fresh path request."""
        Transport.path_table.pop(dest_hash, None)
        Transport.path_states.pop(dest_hash, None)
        log("Expired path to " + dest_hash.hex()[:8], LOG_VERBOSE)

    @staticmethod
    def save_path_table(path):
        """Persist the path table (+ the cached announces it references) to flash
        as JSON, so a rebooted relay isn't a mesh blackout. Interfaces are stored
        by name and re-resolved on load. Throttle calls (flash wear)."""
        try:
            import json
            out = {}
            for dest, e in Transport.path_table.items():
                raw = Transport.get_cached(e[const.IDX_PT_ANNOUNCE])
                if raw is None:
                    continue   # can't restore a path without its announce
                recv = e[const.IDX_PT_RECV_IF]
                out[dest.hex()] = [
                    e[const.IDX_PT_NEXT_HOP].hex(), e[const.IDX_PT_HOPS],
                    e[const.IDX_PT_EXPIRES], (str(recv) if recv is not None else ""),
                    e[const.IDX_PT_ANNOUNCE].hex(), e[const.IDX_PT_EMITTED], raw.hex(),
                ]
            with open(path, "w") as f:
                json.dump(out, f)
            log("Saved " + str(len(out)) + " paths to " + path, LOG_VERBOSE)
        except Exception as e:
            log("save_path_table error: " + str(e), LOG_ERROR)

    @staticmethod
    def load_path_table(path):
        """Reload a persisted path table on boot, dropping already-stale entries
        and re-resolving interfaces by name."""
        try:
            import json
            try:
                f = open(path)
            except OSError:
                return
            with f:
                data = json.load(f)
            now = time.time()
            ifmap = dict((str(i), i) for i in Transport.interfaces)
            count = 0
            for dhex, v in data.items():
                try:
                    expires = v[2]
                    if now >= expires:
                        continue   # already stale
                    dest = bytes.fromhex(dhex)
                    announce_hash = bytes.fromhex(v[4])
                    Transport.path_table[dest] = [
                        now, bytes.fromhex(v[0]), v[1], expires,
                        ifmap.get(v[3]), announce_hash, v[5],
                    ]
                    Transport.packet_cache[announce_hash] = bytes.fromhex(v[6])
                    Transport.reachable_destinations[dest] = now
                    count += 1
                except Exception:
                    continue
            log("Loaded " + str(count) + " paths from " + path, LOG_NOTICE)
        except Exception as e:
            log("load_path_table error: " + str(e), LOG_ERROR)

    @staticmethod
    def _handle_announce(packet):
        from .identity import Identity
        import gc; gc.collect()
        valid = Identity.validate_announce(packet)
        gc.collect()
        if not valid:
            log("Invalid announce for " + packet.destination_hash.hex(), LOG_DEBUG)
            return

        dest = packet.destination_hash
        if dest in Transport.blackholed_identities:
            log("Announce from blackholed " + dest.hex()[:8] + " dropped", LOG_DEBUG)
            return
        now = time.time()
        emitted = Transport._announce_emitted(packet)

        # Next hop toward dest: the relay we heard it from (HDR_2 transport_id),
        # else dest itself (directly reachable).
        if packet.header_type == const.HDR_2 and packet.transport_id:
            received_from = packet.transport_id
        else:
            received_from = dest

        # Don't install a route to one of our OWN destinations (e.g. our own
        # announce echoed back as HDR_2 by a relay).
        from .destination import Destination as _Dest
        is_self = any(
            d.hash == dest and d.direction == _Dest.IN
            for d in Transport.destinations
        )

        # should_add: install/replace a path only for a better hop count, an
        # expired path, or a strictly newer announce emission (replay/echo/loop
        # rejection). Note: a node that loses its clock on reboot re-announces
        # with a smaller emission and is ignored until the path expires or it
        # re-syncs time — acceptable, and self-healing.
        should_add = False
        if not is_self:
            entry = Transport.path_table.get(dest)
            if entry is None:
                should_add = True
            elif packet.hops <= entry[const.IDX_PT_HOPS]:
                should_add = emitted > entry[const.IDX_PT_EMITTED]
            else:
                should_add = (emitted > entry[const.IDX_PT_EMITTED]
                              or now >= entry[const.IDX_PT_EXPIRES])

        if not should_add:
            log("Announce " + dest.hex()[:8] + " ignored (dup/worse/older)", LOG_DEBUG)
            return

        log("Valid announce from " + dest.hex(), LOG_NOTICE)

        # Install the rich path-table entry (evict oldest if full).
        if (len(Transport.path_table) >= const.MAX_PATH_TABLE
                and dest not in Transport.path_table):
            oldest = min(Transport.path_table,
                         key=lambda k: Transport.path_table[k][const.IDX_PT_TIMESTAMP])
            Transport.path_table.pop(oldest, None)
        Transport.path_table[dest] = [now, received_from, packet.hops,
                                      now + const.PATH_EXPIRY,
                                      packet.receiving_interface,
                                      packet.packet_hash, emitted]
        log("Path: " + dest.hex()[:8] + " hops=" + str(packet.hops)
            + " via " + received_from.hex()[:8], LOG_VERBOSE)

        # Cache the announce so we can answer path requests by replaying it.
        Transport.cache_announce(packet.packet_hash, packet.raw)

        # Mark reachable (membership-only) for opportunistic sends / path waiters.
        if (len(Transport.reachable_destinations) >= const.MAX_DESTINATIONS
                and dest not in Transport.reachable_destinations):
            Transport.reachable_destinations.pop(
                next(iter(Transport.reachable_destinations)), None)
        Transport.reachable_destinations[dest] = now

        # Queue a timed rebroadcast (transport nodes only, subject to the
        # per-source announce rate-limit — the path is kept either way).
        if (Transport.transport_enabled and not is_self
                and Transport._announce_rate_ok(dest)):
            Transport._enqueue_announce(packet, received_from)

        # Answer any pending discovery path request for this dest with a targeted
        # path response (overrides the normal rebroadcast entry for this dest).
        pr = Transport.discovery_path_requests.pop(dest, None)
        if pr is not None and pr.get("requesting_interface") is not None:
            Transport._enqueue_path_response(dest, packet.raw,
                                             Transport.path_table[dest],
                                             pr["requesting_interface"])

        # Bootstrap the clock from the announce timestamp (no-op unless time sync
        # is enabled and the clock is still unset).
        if Transport.time_sync_enabled and not Transport._clock_synced:
            try:
                _off = const.KEYSIZE // 8 + const.NAME_HASH_LENGTH // 8
                if packet.data is not None and len(packet.data) >= _off + 10:
                    _ts = int.from_bytes(packet.data[_off + 5:_off + 10], "big")
                    Transport.sync_clock_from(_ts, dest)
            except Exception:
                pass

        # Dispatch to app announce handlers (fires once per new emission).
        app_data = Identity.recall_app_data(dest)
        if app_data:
            log("Announce app_data: " + str(app_data), LOG_VERBOSE)
        for d in Transport.destinations:
            if hasattr(d, '_announce_handler') and d._announce_handler:
                try:
                    d._announce_handler(dest, app_data, packet)
                except Exception as e:
                    log("Announce handler error: " + str(e), LOG_ERROR)

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
        # Transit: if this proof corresponds to a DATA packet we forwarded,
        # route it back along the reverse path (independent of local handling).
        Transport._transit_proof(packet)

        if packet.context == const.CTX_LRPROOF:
            # Transit: relay the link-request proof back toward the initiator.
            if packet.destination_hash in Transport.link_table:
                return Transport._transit_lr_proof(packet)
            # Local pending link
            for link in Transport.pending_links:
                if link.link_id == packet.destination_hash:
                    Transport._cache_packet_hash(packet)   # ours — remember now
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
        """Return known hop count to destination, or 0 if unknown."""
        entry = Transport.path_table.get(destination_hash)
        if entry is not None:
            return entry[const.IDX_PT_HOPS]
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

                # Rebroadcast queued announces (transport mode).
                if Transport.transport_enabled and Transport.announce_table:
                    Transport._service_announce_table()

                # Periodic table maintenance: expiry, offline-interface purge
                # (WiFi flap), discovery timeouts, cache + rate-window cleanup;
                # throttled path-table persistence to flash.
                if now - Transport._last_cull >= const.CULL_INTERVAL:
                    Transport._last_cull = now
                    Transport._cull_tables()
                    if (Transport.persist_path is not None
                            and now - Transport._last_persist >= const.PERSIST_INTERVAL):
                        Transport._last_persist = now
                        Transport.save_path_table(Transport.persist_path)

                import gc
                gc.collect()

            except Exception as e:
                log("Transport job error: " + str(e), LOG_ERROR)

            import uasyncio as asyncio
            await asyncio.sleep(0.25)
