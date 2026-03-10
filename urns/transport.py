# µReticulum Transport
# Supports optional transport mode (blind flood forwarding between interfaces)
# Uses uasyncio instead of threading

import os
import time
from . import const
from .log import log, LOG_VERBOSE, LOG_DEBUG, LOG_ERROR, LOG_EXTREME, LOG_NOTICE, LOG_WARNING

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

    transport_enabled = False

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
        """Forward raw packet to all interfaces except the one it arrived on"""
        hops = raw[1]
        if hops >= const.TRANSPORT_HOPLIMIT:
            log("Forward: hop limit reached (" + str(hops) + "), dropping", LOG_DEBUG)
            return

        fwd = bytearray(raw)
        fwd[1] = hops + 1

        for interface in Transport.interfaces:
            if interface is receiving_interface:
                continue
            if not interface.online:
                continue
            try:
                interface.process_outgoing(fwd)
                log("Forward: " + str(len(fwd)) + "B " + receiving_interface.name + " -> " + interface.name + " hops=" + str(fwd[1]), LOG_DEBUG)
            except Exception as e:
                log("Forward error on " + interface.name + ": " + str(e), LOG_ERROR)

    @staticmethod
    def inbound(raw, interface=None):
        """Process an incoming raw packet from an interface"""
        from .packet import Packet
        from .identity import Identity

        try:
            if len(raw) < 2:
                return

            log("Inbound: " + str(len(raw)) + " bytes, flags=0x" + ("%02x" % raw[0]), LOG_EXTREME)

            # Drop IFAC-tagged packets (bit 7 set). µReticulum does not
            # implement IFAC, so these cannot be decoded. Per reference RNS,
            # interfaces without IFAC must drop IFAC-flagged packets.
            if raw[0] & 0x80:
                log("Inbound: IFAC flag set, dropping (IFAC not supported)", LOG_DEBUG)
                return

            packet = Packet(destination=None, data=raw)
            if not packet.unpack():
                log("Inbound: unpack failed", LOG_DEBUG)
                return

            log("Inbound: type=" + str(packet.packet_type) + " dest=" + packet.destination_hash.hex(), LOG_DEBUG)

            packet.receiving_interface = interface
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
    def _handle_announce(packet):
        from .identity import Identity
        import gc; gc.collect()
        valid = Identity.validate_announce(packet)
        gc.collect()
        if valid:
            log("Valid announce from " + packet.destination_hash.hex(), LOG_NOTICE)

            # Record transport path from HDR_2 announces so outbound
            # DATA packets can be routed via the transport node.
            if packet.header_type == const.HDR_2 and packet.transport_id:
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
        for dest in Transport.destinations:
            if dest.hash == packet.destination_hash:
                dest.receive(packet)
                return True
        return False

    @staticmethod
    def _handle_data(packet):
        for dest in Transport.destinations:
            if dest.hash == packet.destination_hash:
                import gc; gc.collect()
                dest.receive(packet)
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

                import gc
                gc.collect()

            except Exception as e:
                log("Transport job error: " + str(e), LOG_ERROR)

            import uasyncio as asyncio
            await asyncio.sleep(0.25)
