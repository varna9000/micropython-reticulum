# Transport routing tests (host-side, crypto-free). Run:
#   python3 firmware/tests/test_transport.py
#
# Add routing assertions here as each implementation phase lands. Phase 0
# covers the additive foundations (transmit/cache helpers, constants).

import harness
from harness import (const, transport, packet, Transport, MockInterface, Identity,
                     reset_transport, set_identity,
                     build_announce_hdr1, build_announce_hdr2, build_announce_data,
                     build_data_hdr1, build_data_hdr2, build_proof,
                     build_linkrequest_hdr2, build_lrproof, parse_signalling)

ME = b"\xAA" * 16
DEST = b"\xD0" * 16
RELAY = b"\xBB" * 16


def _mkpkt(raw, hops_inc=1):
    p = packet.Packet(None, raw)
    assert p.unpack()
    p.hops += hops_inc
    return p


# ----------------------------- Phase 0 -----------------------------------
def test_constants_present():
    # Index + cap + timing constants must exist with sane values.
    assert const.IDX_PT_NEXT_HOP == 1
    assert const.IDX_PT_HOPS == 2
    assert const.IDX_LT_VALIDATED == 7
    assert const.MAX_HOPS >= 8
    assert const.MAX_REVERSE_TABLE >= 1
    assert 0.0 < const.ANNOUNCE_CAP < 1.0
    assert const.PATHFINDER_RW > 0.0


def test_transmit_directed():
    reset_transport()
    a = MockInterface("a")
    b = MockInterface("b", online=False)
    # Online interface: forwards and records exact bytes.
    assert Transport.transmit(a, b"\x01\x02\x03") is True
    assert a.sent == [b"\x01\x02\x03"]
    # Offline interface: refused.
    assert Transport.transmit(b, b"\xff") is False
    assert b.sent == []
    # None interface: refused.
    assert Transport.transmit(None, b"\xff") is False
    # OUT=False gating: refused.
    a.OUT = False
    assert Transport.transmit(a, b"\x09") is False
    assert a.sent == [b"\x01\x02\x03"]  # unchanged


def test_transmit_swallows_iface_errors():
    reset_transport()

    class Boom(MockInterface):
        def process_outgoing(self, data):
            raise OSError("radio fault")

    bad = Boom("bad")
    # Must not propagate — a relay can't crash because one egress failed.
    assert Transport.transmit(bad, b"\x01") is False


def test_cache_announce_lru_bound():
    reset_transport()
    n = const.MAX_PACKET_CACHE + 8
    for i in range(n):
        key = bytes([i & 0xFF]) + b"\x00" * 31
        Transport.cache_announce(key, b"raw" + bytes([i & 0xFF]))
    assert len(Transport.packet_cache) <= const.MAX_PACKET_CACHE
    # Idempotent: re-caching a present key doesn't grow or overwrite.
    before = len(Transport.packet_cache)
    some_key = next(iter(Transport.packet_cache))
    some_val = Transport.packet_cache[some_key]
    Transport.cache_announce(some_key, b"DIFFERENT")
    assert len(Transport.packet_cache) == before
    assert Transport.packet_cache[some_key] == some_val
    assert Transport.get_cached(some_key) == some_val
    assert Transport.get_cached(b"\xaa" * 32) is None


# ----------------------------- Phase 1: announces ------------------------
def test_announce_hdr1_installs_direct_path():
    reset_transport()
    iface = MockInterface("lora")
    Transport.interfaces = [iface]
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)), iface)
    e = Transport.path_table[DEST]
    assert e[const.IDX_PT_NEXT_HOP] == DEST          # HDR_1 -> next hop is dest itself
    assert e[const.IDX_PT_HOPS] == 1                 # on-wire 0 -> +1
    assert e[const.IDX_PT_RECV_IF] is iface
    assert e[const.IDX_PT_EMITTED] == 1000
    assert Transport.hops_to(DEST) == 1


def test_announce_hdr2_records_via_transport():
    reset_transport()
    iface = MockInterface("wifi")
    Transport.interfaces = [iface]
    raw = build_announce_hdr2(RELAY, DEST, data=build_announce_data(emitted=1000), hops=2)
    Transport.inbound(raw, iface)
    e = Transport.path_table[DEST]
    assert e[const.IDX_PT_NEXT_HOP] == RELAY         # next hop is the relay
    assert e[const.IDX_PT_HOPS] == 3                 # on-wire 2 -> +1


def test_announce_rebroadcast_as_hdr2():
    reset_transport()
    lora = MockInterface("lora")
    wifi = MockInterface("wifi")
    Transport.interfaces = [lora, wifi]
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)), lora)
    assert DEST in Transport.announce_table
    Transport.announce_table[DEST][const.IDX_AT_RTMO] = 0   # fire now
    Transport._service_announce_table()
    out = (lora.sent + wifi.sent)
    assert out, "expected a rebroadcast"
    o = out[0]
    assert (o[0] & 0x40) != 0      # HDR_2 set
    assert (o[0] & 0x10) != 0      # TRANSPORT set
    assert o[1] == 1               # hops = stored (1); next node will +1
    assert o[2:18] == ME           # OUR transport_id stamped
    assert o[18:34] == DEST        # destination preserved


def test_should_add_rejects_echo_worse_hops():
    # Multi-relay no-storm: a longer path for the SAME announce emission is ignored.
    reset_transport()
    iface = MockInterface("lora")
    Transport.interfaces = [iface]
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)), iface)
    assert Transport.path_table[DEST][const.IDX_PT_HOPS] == 1
    # Same emission, arriving via a relay at more hops -> must NOT replace.
    Transport.inbound(build_announce_hdr2(RELAY, DEST,
                      data=build_announce_data(emitted=1000), hops=2), iface)
    assert Transport.path_table[DEST][const.IDX_PT_HOPS] == 1
    assert Transport.path_table[DEST][const.IDX_PT_NEXT_HOP] == DEST


def test_should_add_newer_emission_replaces():
    reset_transport()
    iface = MockInterface("lora")
    Transport.interfaces = [iface]
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)), iface)
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=2000)), iface)
    assert Transport.path_table[DEST][const.IDX_PT_EMITTED] == 2000


def test_should_add_better_hops_same_emission_replaces():
    # Regression (LoRa-vs-TCP path stick): a SHORTER path for the SAME announce
    # emission MUST displace a longer one already installed. Field case: a 1-hop
    # TCP copy of an announce first heard over a 3-hop LoRa loop was rejected
    # because both share an emission timestamp, pinning the route to lossy LoRa.
    reset_transport()
    iface = MockInterface("lora")
    Transport.interfaces = [iface]
    # Learn it the long way first: via a relay at 3 hops (on-wire 2 -> +1).
    Transport.inbound(build_announce_hdr2(RELAY, DEST,
                      data=build_announce_data(emitted=1000), hops=2), iface)
    assert Transport.path_table[DEST][const.IDX_PT_HOPS] == 3
    assert Transport.path_table[DEST][const.IDX_PT_NEXT_HOP] == RELAY
    # Same emission arrives via a SHORTER path (direct, 1 hop) -> must replace.
    Transport.inbound(build_announce_hdr1(DEST,
                      data=build_announce_data(emitted=1000)), iface)
    assert Transport.path_table[DEST][const.IDX_PT_HOPS] == 1
    assert Transport.path_table[DEST][const.IDX_PT_NEXT_HOP] == DEST


def test_should_add_better_hops_stale_emission_adopts():
    # Field case (c4756060): a strictly SHORTER path must be adopted even when its
    # announce is OLDER. The peer replays a cached (stale-emission) 1-hop copy while
    # fresh copies keep arriving over the longer route; an emission gate wrongly
    # pinned routing to the lossy 3-hop path. A shorter path always wins.
    reset_transport()
    iface = MockInterface("lora")
    Transport.interfaces = [iface]
    Transport.inbound(build_announce_hdr2(RELAY, DEST,
                      data=build_announce_data(emitted=2000), hops=2), iface)
    assert Transport.path_table[DEST][const.IDX_PT_HOPS] == 3
    # Fewer hops but an OLDER emission (1000 < 2000) -> STILL adopt (shorter wins).
    Transport.inbound(build_announce_hdr1(DEST,
                      data=build_announce_data(emitted=1000)), iface)
    assert Transport.path_table[DEST][const.IDX_PT_HOPS] == 1
    assert Transport.path_table[DEST][const.IDX_PT_NEXT_HOP] == DEST


def test_should_add_longer_path_no_flipflop():
    # Once a shorter path is installed, a LONGER path must NOT displace it just for
    # carrying a newer emission (that flip-flops the route every announce cycle).
    # Only expiry of the shorter path lets a longer one back in (failover).
    reset_transport()
    iface = MockInterface("lora")
    Transport.interfaces = [iface]
    Transport.inbound(build_announce_hdr1(DEST,
                      data=build_announce_data(emitted=1000)), iface)   # short, 1 hop
    assert Transport.path_table[DEST][const.IDX_PT_HOPS] == 1
    # Longer (3-hop) path, NEWER emission -> rejected while the short path is valid.
    Transport.inbound(build_announce_hdr2(RELAY, DEST,
                      data=build_announce_data(emitted=5000), hops=2), iface)
    assert Transport.path_table[DEST][const.IDX_PT_HOPS] == 1
    assert Transport.path_table[DEST][const.IDX_PT_NEXT_HOP] == DEST
    # Force-expire the short path -> the longer path is now accepted (failover).
    Transport.path_table[DEST][const.IDX_PT_EXPIRES] = 0
    Transport.inbound(build_announce_hdr2(RELAY, DEST,
                      data=build_announce_data(emitted=6000), hops=2), iface)
    assert Transport.path_table[DEST][const.IDX_PT_HOPS] == 3


def test_should_add_reconfirm_refreshes_expiry():
    # Keep-alive: a re-confirming announce for the SAME next hop (even with an
    # unchanged/stale emission) refreshes expiry, so a valid route isn't culled
    # when the peer replays a constant-emission cached announce (c4756060's TCP peer).
    reset_transport()
    iface = MockInterface("lora")
    Transport.interfaces = [iface]
    Transport.inbound(build_announce_hdr2(RELAY, DEST,
                      data=build_announce_data(emitted=1000), hops=2), iface)
    e = Transport.path_table[DEST]
    assert e[const.IDX_PT_HOPS] == 3
    e[const.IDX_PT_EXPIRES] = 1.0                    # pretend it's about to expire
    Transport.packet_hashlist.clear()                # let the same announce be seen again
    Transport.inbound(build_announce_hdr2(RELAY, DEST,
                      data=build_announce_data(emitted=1000), hops=2), iface)
    e = Transport.path_table[DEST]
    assert e[const.IDX_PT_HOPS] == 3                 # route unchanged
    assert e[const.IDX_PT_NEXT_HOP] == RELAY
    assert e[const.IDX_PT_EXPIRES] > 1.0             # expiry refreshed (kept alive)


def test_duplicate_announce_no_restorm():
    reset_transport()
    iface = MockInterface("lora")
    Transport.interfaces = [iface]
    raw = build_announce_hdr1(DEST, data=build_announce_data(emitted=1000))
    Transport.inbound(raw, iface)
    Transport.inbound(raw, iface)            # exact duplicate
    assert len(Transport.announce_table) == 1
    assert Transport.announce_table[DEST][const.IDX_AT_RETRIES] == 0


def test_loop_self_dedup():
    # The relay's own rebroadcast echoing back (shared medium) must not install
    # a path-to-self or re-enqueue — the route-independent hash + should_add stop it.
    reset_transport()
    lora = MockInterface("lora")
    wifi = MockInterface("wifi")
    Transport.interfaces = [lora, wifi]
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)), lora)
    Transport.announce_table[DEST][const.IDX_AT_RTMO] = 0
    Transport._service_announce_table()
    echo = (lora.sent + wifi.sent)[0]
    Transport.inbound(echo, wifi)            # feed our own rebroadcast back
    e = Transport.path_table[DEST]
    assert e[const.IDX_PT_HOPS] == 1         # unchanged
    assert e[const.IDX_PT_NEXT_HOP] == DEST  # still direct, NOT via ourselves


# ----------------------------- Phase 1: packet_filter --------------------
def test_filter_drops_other_relay_transit():
    reset_transport()
    other = _mkpkt(build_data_hdr2(b"\xCC" * 16, DEST))
    assert Transport.packet_filter(other) is False
    mine = _mkpkt(build_data_hdr2(ME, DEST))
    assert Transport.packet_filter(mine) is True


def test_filter_plain_hop_cap():
    reset_transport()
    p1 = _mkpkt(build_data_hdr1(DEST, dest_type=const.DEST_PLAIN), hops_inc=0)
    p1.hops = 1
    assert Transport.packet_filter(p1) is True
    p1.hops = 2
    assert Transport.packet_filter(p1) is False


def test_filter_resource_bypasses_dedup():
    reset_transport()
    p = _mkpkt(build_data_hdr1(DEST, context=const.CTX_RESOURCE))
    Transport._cache_packet_hash(p)                      # pretend seen
    assert Transport.packet_filter(p) is True            # resource ctx still passes


def test_filter_dedups_data():
    reset_transport()
    raw = build_data_hdr1(DEST)
    p = _mkpkt(raw)
    assert Transport.packet_filter(p) is True
    Transport._cache_packet_hash(p)
    assert Transport.packet_filter(_mkpkt(raw)) is False


# ----------------------------- Phase 2: transit forwarding ---------------
def _path(next_hop, hops, out_if):
    # [TIMESTAMP, NEXT_HOP, HOPS, EXPIRES, RECV_IF, ANNOUNCE, EMITTED]
    return [1000.0, next_hop, hops, 9.0e9, out_if, b"\x00" * 32, 1000]


class _LocalDest:
    def __init__(self, h):
        self.hash = h
        self.direction = 0x11      # Destination.IN
        self.type = 0x00
        self.proof_strategy = 0x21  # PROVE_NONE
        self.received = []

    def receive(self, packet):
        self.received.append(packet)
        return True


def test_transit_data_forward_multihop():
    reset_transport()
    lora = MockInterface("lora")
    wifi = MockInterface("wifi")
    Transport.interfaces = [lora, wifi]
    Transport.path_table[DEST] = _path(RELAY, 3, wifi)   # dest via RELAY, 3 hops, on wifi
    raw = build_data_hdr2(ME, DEST, ciphertext=b"\xAB" * 16, hops=0)
    trunc = _mkpkt(raw, hops_inc=0).getTruncatedHash()
    Transport.inbound(raw, lora)                          # arrives on lora, transit out wifi
    assert len(wifi.sent) == 1 and lora.sent == []
    out = wifi.sent[0]
    assert (out[0] & 0x40) != 0          # still HDR_2
    assert out[1] == 1                   # hops 0 -> 1
    assert out[2:18] == RELAY            # transport_id swapped to next hop
    assert out[18:34] == DEST
    rt = Transport.reverse_table[trunc]
    assert rt[const.IDX_RT_RECV_IF] is lora
    assert rt[const.IDX_RT_OUTB_IF] is wifi


def test_transit_data_last_hop_strips_to_hdr1():
    reset_transport()
    lora = MockInterface("lora")
    wifi = MockInterface("wifi")
    Transport.interfaces = [lora, wifi]
    Transport.path_table[DEST] = _path(RELAY, 1, wifi)   # final hop
    Transport.inbound(build_data_hdr2(ME, DEST, hops=0), lora)
    out = wifi.sent[0]
    assert (out[0] & 0x40) == 0          # HDR_1 (transport headers stripped)
    assert (out[0] & 0x10) == 0          # BROADCAST
    assert out[2:18] == DEST             # dest now right after the 2-byte header
    assert out[1] == 1


def test_transit_no_path_drops():
    reset_transport()
    lora = MockInterface("lora")
    wifi = MockInterface("wifi")
    Transport.interfaces = [lora, wifi]
    Transport.inbound(build_data_hdr2(ME, DEST, hops=0), lora)   # no path entry
    assert lora.sent == [] and wifi.sent == []
    assert not Transport.reverse_table


def test_transit_self_dest_delivers_locally():
    reset_transport()
    lora = MockInterface("lora")
    wifi = MockInterface("wifi")
    Transport.interfaces = [lora, wifi]
    ld = _LocalDest(DEST)
    Transport.destinations = [ld]
    Transport.path_table[DEST] = _path(RELAY, 3, wifi)   # even with a stray path...
    Transport.inbound(build_data_hdr2(ME, DEST, hops=0), lora)
    assert len(ld.received) == 1         # ...the self-dest guard delivers locally
    assert wifi.sent == []               # and does NOT forward


def test_transit_proof_returns_along_reverse_path():
    reset_transport()
    lora = MockInterface("lora")
    wifi = MockInterface("wifi")
    Transport.interfaces = [lora, wifi]
    Transport.path_table[DEST] = _path(RELAY, 3, wifi)
    data_raw = build_data_hdr2(ME, DEST, ciphertext=b"\xAB" * 16, hops=0)
    trunc = _mkpkt(data_raw, hops_inc=0).getTruncatedHash()
    Transport.inbound(data_raw, lora)
    assert trunc in Transport.reverse_table
    Transport.inbound(build_proof(trunc), wifi)          # proof back on the out interface
    assert len(lora.sent) == 1                           # routed back toward the source
    assert lora.sent[0][2:18] == trunc
    assert trunc not in Transport.reverse_table          # consumed


def test_transit_proof_wrong_interface_not_forwarded():
    reset_transport()
    lora = MockInterface("lora")
    wifi = MockInterface("wifi")
    Transport.interfaces = [lora, wifi]
    Transport.path_table[DEST] = _path(RELAY, 3, wifi)
    data_raw = build_data_hdr2(ME, DEST, hops=0)
    trunc = _mkpkt(data_raw, hops_inc=0).getTruncatedHash()
    Transport.inbound(data_raw, lora)
    lora.sent.clear()
    wifi.sent.clear()
    Transport.inbound(build_proof(trunc), lora)          # WRONG interface
    assert lora.sent == [] and wifi.sent == []
    assert trunc in Transport.reverse_table              # NOT consumed


LINK_ID = b"\x77" * 16


def _link_entry(nh_if, recv_if, rem_hops=1, hops=1, dest=DEST, validated=True):
    # [ts, next_hop, nh_if, rem_hops, recv_if, hops, dest, validated, proof_tmo]
    return [1000.0, RELAY, nh_if, rem_hops, recv_if, hops, dest, validated, 9.0e9]


# ----------------------------- Phase 3: link transit ---------------------
def test_transit_linkrequest_builds_link_table():
    reset_transport()
    wifi = MockInterface("wifi", hw_mtu=1064)
    lora = MockInterface("lora", hw_mtu=508)
    Transport.interfaces = [wifi, lora]
    Transport.path_table[DEST] = _path(RELAY, 1, lora)   # server via RELAY, 1 hop, egress lora
    Transport.inbound(build_linkrequest_hdr2(ME, DEST, mtu=500, hops=0), wifi)
    assert len(lora.sent) == 1                            # forwarded toward server
    assert len(Transport.link_table) == 1
    entry = list(Transport.link_table.values())[0]
    assert entry[const.IDX_LT_NH_IF] is lora             # toward server
    assert entry[const.IDX_LT_RECV_IF] is wifi           # toward initiator
    assert entry[const.IDX_LT_REM_HOPS] == 1
    assert entry[const.IDX_LT_HOPS] == 1                 # LR taken hops (0 -> 1)
    assert entry[const.IDX_LT_DEST] == DEST
    assert entry[const.IDX_LT_VALIDATED] is False


def test_transit_linkrequest_clamps_mtu():
    reset_transport()
    wifi = MockInterface("wifi", hw_mtu=1064)
    lora = MockInterface("lora", hw_mtu=508)
    Transport.interfaces = [wifi, lora]
    Transport.path_table[DEST] = _path(RELAY, 1, lora)   # egress lora (HW_MTU 508)
    Transport.inbound(build_linkrequest_hdr2(ME, DEST, mtu=1000, hops=0), wifi)
    out = lora.sent[0]
    mtu, _mode = parse_signalling(out[-3:])              # clamped to the lora bottleneck
    assert mtu == 508


def test_link_id_signalling_independent():
    reset_transport()
    a = build_linkrequest_hdr2(ME, DEST, mtu=1000)
    b = build_linkrequest_hdr2(ME, DEST, mtu=200)        # different MTU signalling
    c = build_linkrequest_hdr2(ME, DEST, eph_pub=b"\x99" * 64, mtu=1000)  # different keys
    pa = _mkpkt(a, 0); pb = _mkpkt(b, 0); pc = _mkpkt(c, 0)
    assert Transport._link_id_from_lr(pa) == Transport._link_id_from_lr(pb)   # MTU-independent
    assert Transport._link_id_from_lr(pa) != Transport._link_id_from_lr(pc)   # key-sensitive


def test_transit_link_forwards_both_directions():
    reset_transport()
    wifi = MockInterface("wifi")
    lora = MockInterface("lora")
    Transport.interfaces = [wifi, lora]
    Transport.link_table[LINK_ID] = _link_entry(nh_if=lora, recv_if=wifi)
    # From initiator: arrives on recv_if (wifi), hops match HOPS -> egress nh_if (lora).
    Transport.inbound(build_data_hdr1(LINK_ID, ciphertext=b"\x01" * 16, hops=0, dest_type=0x03), wifi)
    assert len(lora.sent) == 1 and wifi.sent == []
    lora.sent.clear()
    # From server: arrives on nh_if (lora), hops match REM_HOPS -> egress recv_if (wifi).
    Transport.inbound(build_data_hdr1(LINK_ID, ciphertext=b"\x02" * 16, hops=0, dest_type=0x03), lora)
    assert len(wifi.sent) == 1


def test_transit_link_hop_mismatch_dropped():
    reset_transport()
    wifi = MockInterface("wifi")
    lora = MockInterface("lora")
    Transport.interfaces = [wifi, lora]
    Transport.link_table[LINK_ID] = _link_entry(nh_if=lora, recv_if=wifi)
    # Arrives on wifi but with hops=2 (expected HOPS=1) -> dropped, not forwarded.
    Transport.inbound(build_data_hdr1(LINK_ID, hops=1, dest_type=0x03), wifi)
    assert lora.sent == [] and wifi.sent == []


def test_transit_lrproof_forwards_back_and_validates():
    reset_transport()
    wifi = MockInterface("wifi")
    lora = MockInterface("lora")
    Transport.interfaces = [wifi, lora]
    Transport.link_table[LINK_ID] = _link_entry(nh_if=lora, recv_if=wifi, validated=False)
    # LRPROOF returns from the server side (nh_if=lora) with hops == REM_HOPS.
    Transport.inbound(build_lrproof(LINK_ID, hops=0), lora)
    assert len(wifi.sent) == 1                            # forwarded back to initiator
    assert Transport.link_table[LINK_ID][const.IDX_LT_VALIDATED] is True


def test_transit_lrproof_wrong_interface_dropped():
    reset_transport()
    wifi = MockInterface("wifi")
    lora = MockInterface("lora")
    Transport.interfaces = [wifi, lora]
    Transport.link_table[LINK_ID] = _link_entry(nh_if=lora, recv_if=wifi, validated=False)
    Transport.inbound(build_lrproof(LINK_ID, hops=0), wifi)   # wrong side (recv_if, not nh_if)
    assert wifi.sent == [] and lora.sent == []
    assert Transport.link_table[LINK_ID][const.IDX_LT_VALIDATED] is False


# ----------------------------- Phase 4: path requests + egress -----------
class _MockPacket:
    def __init__(self, raw, dest_hash, ptype=0x00):
        self.raw = raw
        self.destination_hash = dest_hash
        self.packet_type = ptype
        self.attached_interface = None
        self.sent = False
        self.sent_at = None
        self.create_receipt = False
        self.receipt = None

    def get_hash(self):
        return Identity.full_hash(self.raw)


def _req(iface):
    return type("P", (), {"receiving_interface": iface})()


def test_path_request_answered_from_cache():
    reset_transport()
    wifi = MockInterface("wifi")
    lora = MockInterface("lora")
    Transport.interfaces = [wifi, lora]
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)), lora)
    Transport.path_request_handler(DEST + b"\x88" * 16, _req(wifi))   # dest + tag, on wifi
    entry = Transport.announce_table[DEST]
    assert entry[const.IDX_AT_BLK_RBRD] is True
    assert entry[const.IDX_AT_ATTCHD_IF] is wifi
    Transport._service_announce_table()
    assert len(wifi.sent) == 1 and lora.sent == []                    # only the requester's iface
    assert wifi.sent[0][34] == const.CTX_PATH_RESPONSE               # PATH_RESPONSE context byte


def test_path_request_dedup():
    reset_transport()
    wifi = MockInterface("wifi")
    lora = MockInterface("lora")
    Transport.interfaces = [wifi, lora]
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)), lora)
    tag = b"\x88" * 16
    Transport.path_request_handler(DEST + tag, _req(wifi))
    Transport.announce_table.clear()                                 # consume the first answer
    Transport.path_request_handler(DEST + tag, _req(wifi))           # same (dest+tag) again
    assert DEST not in Transport.announce_table                      # ignored as duplicate


def test_path_request_recursive_discovery():
    reset_transport()
    wifi = MockInterface("wifi")
    lora = MockInterface("lora")
    Transport.interfaces = [wifi, lora]
    Transport.path_request_handler(DEST + b"\x99" * 16, _req(wifi))  # no path known
    assert DEST in Transport.discovery_path_requests
    assert Transport.discovery_path_requests[DEST]["requesting_interface"] is wifi


def test_discovery_answered_when_announce_arrives():
    reset_transport()
    wifi = MockInterface("wifi")
    lora = MockInterface("lora")
    Transport.interfaces = [wifi, lora]
    # A discovery request is pending (requester on wifi); the announce then arrives on lora.
    Transport.discovery_path_requests[DEST] = {"requesting_interface": wifi, "timeout": 9.0e9}
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)), lora)
    assert DEST not in Transport.discovery_path_requests             # consumed
    entry = Transport.announce_table[DEST]
    assert entry[const.IDX_AT_BLK_RBRD] is True                      # targeted path response
    assert entry[const.IDX_AT_ATTCHD_IF] is wifi


def test_outbound_directed_egress_multihop():
    reset_transport()
    wifi = MockInterface("wifi")
    lora = MockInterface("lora")
    Transport.interfaces = [wifi, lora]
    Transport.path_table[DEST] = _path(RELAY, 3, lora)               # 3 hops via lora
    Transport.outbound(_MockPacket(b"\x50" + b"\x00" * 40, DEST))
    assert len(lora.sent) == 1 and wifi.sent == []                  # directed to path iface only


def test_outbound_broadcasts_when_direct():
    reset_transport()
    wifi = MockInterface("wifi")
    lora = MockInterface("lora")
    Transport.interfaces = [wifi, lora]
    Transport.path_table[DEST] = _path(DEST, 1, lora)               # 1 hop (directly reachable)
    Transport.outbound(_MockPacket(b"\x50" + b"\x00" * 40, DEST))
    assert len(lora.sent) == 1 and len(wifi.sent) == 1             # broadcast on all


# ----------------------------- Phase 5/6: maintenance --------------------
def test_cull_expires_stale_entries():
    reset_transport()
    lora = MockInterface("lora")
    Transport.interfaces = [lora]
    Transport.path_table[DEST] = [1000.0, RELAY, 2, 1001.0, lora, b"\x00" * 32, 1000]  # EXPIRES past
    Transport.reverse_table[b"\x01" * 16] = [lora, lora, 1000.0]                        # old ts
    Transport.link_table[LINK_ID] = _link_entry(nh_if=lora, recv_if=lora)              # ts=1000 (old)
    Transport.discovery_path_requests[DEST] = {"requesting_interface": lora, "timeout": 1000.0}
    Transport._cull_tables()
    assert DEST not in Transport.path_table
    assert b"\x01" * 16 not in Transport.reverse_table
    assert LINK_ID not in Transport.link_table
    assert DEST not in Transport.discovery_path_requests


def test_cull_purges_offline_interface():
    # WiFi flap: fresh (non-expired) entries via an interface that just went offline.
    reset_transport()
    wifi = MockInterface("wifi")
    lora = MockInterface("lora")
    Transport.interfaces = [wifi, lora]
    Transport.path_table[DEST] = [9.9e9, RELAY, 2, 9.9e9, wifi, b"\x00" * 32, 1000]
    Transport.reverse_table[b"\x01" * 16] = [wifi, lora, 9.9e9]
    Transport.link_table[LINK_ID] = [9.9e9, RELAY, wifi, 1, lora, 1, DEST, True, 9.9e9]
    wifi.online = False                                       # interface drops
    Transport._cull_tables()
    assert DEST not in Transport.path_table                   # route via wifi purged
    assert b"\x01" * 16 not in Transport.reverse_table
    assert LINK_ID not in Transport.link_table


def test_blackhole_drops_announce():
    reset_transport()
    lora = MockInterface("lora")
    Transport.interfaces = [lora]
    Transport.blackhole(DEST)
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)), lora)
    assert DEST not in Transport.path_table                   # announce dropped


def test_blackhole_drops_transit():
    reset_transport()
    wifi = MockInterface("wifi")
    lora = MockInterface("lora")
    Transport.interfaces = [wifi, lora]
    Transport.path_table[DEST] = _path(RELAY, 3, lora)
    Transport.blackhole(DEST)
    Transport.inbound(build_data_hdr2(ME, DEST, hops=0), wifi)
    assert lora.sent == [] and wifi.sent == []               # transit packet dropped


def test_announce_rate_limit():
    reset_transport()
    results = [Transport._announce_rate_ok(DEST) for _ in range(const.ANNOUNCE_RATE_MAX + 2)]
    assert all(results[:const.ANNOUNCE_RATE_MAX])            # first MAX allowed
    assert results[const.ANNOUNCE_RATE_MAX] is False         # throttled after MAX


def test_expire_path():
    reset_transport()
    Transport.path_table[DEST] = _path(RELAY, 2, MockInterface("x"))
    Transport.expire_path(DEST)
    assert DEST not in Transport.path_table


def test_persistence_roundtrip():
    import tempfile
    import os as _os
    reset_transport()
    lora = MockInterface("lora")
    Transport.interfaces = [lora]
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)), lora)
    assert DEST in Transport.path_table
    path = tempfile.mktemp(suffix="_pathtable")
    Transport.save_path_table(path)
    # Wipe in-memory state, then reload from flash.
    Transport.path_table = {}
    Transport.packet_cache = {}
    Transport.reachable_destinations = {}
    Transport.load_path_table(path)
    _os.unlink(path)
    assert DEST in Transport.path_table
    e = Transport.path_table[DEST]
    assert e[const.IDX_PT_NEXT_HOP] == DEST                  # next hop restored
    assert e[const.IDX_PT_HOPS] == 1
    assert e[const.IDX_PT_RECV_IF] is lora                   # interface re-resolved by name
    assert e[const.IDX_PT_EMITTED] == 1000
    # The referenced announce is back in the cache (so path requests can be answered).
    assert Transport.get_cached(e[const.IDX_PT_ANNOUNCE]) is not None


def test_announce_handler_registry():
    # register_announce_handler: fires per new emission with (dest, app_data,
    # packet); dedups registration; a raising handler doesn't break processing.
    reset_transport()
    iface = MockInterface("a")
    Transport.interfaces = [iface]

    calls = []

    def h(dest, app_data, pkt):
        calls.append((dest, app_data, pkt.hops))

    Transport.register_announce_handler(h)
    Transport.register_announce_handler(h)          # no duplicate entry
    assert Transport.announce_handlers == [h]

    Identity.app_data[DEST] = b"Alice"
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)), iface)
    assert calls == [(DEST, b"Alice", 1)]

    # Duplicate emission is ignored -> handler must NOT fire again.
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=1000)), iface)
    assert len(calls) == 1

    # A handler that raises must not break announce processing or later handlers.
    def boom(dest, app_data, pkt):
        raise RuntimeError("boom")

    Transport.announce_handlers.insert(0, boom)
    Transport.inbound(build_announce_hdr1(DEST, data=build_announce_data(emitted=2000)), iface)
    assert len(calls) == 2
    assert DEST in Transport.path_table

    Transport.deregister_announce_handler(h)
    assert Transport.announce_handlers == [boom]


# ------------- HDR_2 transport-header insertion (hop-count rule) -----------
# Reference RNS Transport.outbound inserts transport headers ONLY when
# hops > 1. At hops == 1 the destination is reachable directly on the next-hop
# interface, so the packet must stay HDR_1 -- even though the path entry's
# next_hop is a transport node's identity rather than the destination itself.
# That is the case for EVERY destination behind a shared instance (rnsh,
# nomadnet behind rnsd). Wrapping such a send addresses it "via <transport>";
# the shared instance passes it to its local client with the header attached,
# and the client drops it, because it only accepts a LINKREQUEST whose
# transport_id is None or matches its OWN (ephemeral) transport identity.
# Result: no LRPROOF, nothing logged. Two earlier attempts at this rule used
# "next_hop != destination_hash", which is wrong for exactly this case.

class _StubDest:
    type = const.DEST_SINGLE

    def __init__(self, h):
        self.hash = h
        self.hexhash = h.hex()


def _set_path(dest, next_hop, hops):
    reset_transport()
    Transport.path_table[dest] = [0, next_hop, hops, 0, None, b"", 0]


def test_pack_stays_hdr1_at_one_hop_via_transport_node():
    # rnsh behind rnsd: hops == 1 but next_hop is the instance, not the dest.
    _set_path(DEST, RELAY, 1)
    p = packet.Packet(_StubDest(DEST), b"\x11" * 67, const.PKT_LINKREQUEST)
    p.pack()
    assert p.header_type == const.HDR_1, "hops==1 must not be wrapped in HDR_2"
    assert p.transport_id is None
    assert (p.raw[0] >> 6) & 0x01 == 0
    # HDR_1 layout: flags+hops+dest(16)+context, no 16-byte transport_id.
    assert p.raw[2:18] == DEST
    assert len(p.raw) == 2 + 16 + 1 + 67


def test_pack_wraps_hdr2_beyond_one_hop():
    _set_path(DEST, RELAY, 2)
    p = packet.Packet(_StubDest(DEST), b"\x11" * 67, const.PKT_LINKREQUEST)
    p.pack()
    assert p.header_type == const.HDR_2
    assert p.transport_id == RELAY
    # The TRANSPORT type bit must be set alongside HDR_2, as reference RNS does.
    assert (p.raw[0] >> 4) & 0x01 == 1, "TRANSPORT bit must accompany HDR_2"
    assert p.raw[2:18] == RELAY and p.raw[18:34] == DEST
    assert len(p.raw) == 2 + 16 + 16 + 1 + 67


def test_pack_stays_hdr1_with_no_path():
    _set_path(DEST, RELAY, 2)
    del Transport.path_table[DEST]
    p = packet.Packet(_StubDest(DEST), b"\x11" * 67, const.PKT_LINKREQUEST)
    p.pack()
    assert p.header_type == const.HDR_1 and p.transport_id is None


def _mk_tcp_iface():
    import importlib
    tcp = importlib.import_module("urns.interfaces.tcp")

    class _Sock:
        def __init__(self):
            self.sent = b""

        def settimeout(self, t):
            pass

        def sendall(self, d):
            self.sent += d

    iface = object.__new__(tcp.TCPClientInterface)
    iface.online = True
    iface._socket = _Sock()
    iface.ifac_signing_key = None
    iface.txb = 0
    iface.tx = 0
    iface._last_activity = 0
    iface.name = "tcp-test"
    return iface


def _hdr1_linkrequest():
    # flags: HDR_1, BROADCAST, SINGLE, PKT_LINKREQUEST
    return bytes([0x02, 0]) + DEST + bytes([0x00]) + b"\x11" * 67


def test_tcp_egress_does_not_wrap_at_one_hop():
    # The router strips a relayed request to HDR_1 for the final hop; the TCP
    # interface must not put the transport header straight back on.
    _set_path(DEST, RELAY, 1)
    iface = _mk_tcp_iface()
    raw = _hdr1_linkrequest()
    assert iface.process_outgoing(raw) is True
    body = iface._socket.sent[1:-1]          # strip HDLC flags
    assert body == raw, "hops==1 must go out unmodified as HDR_1"


def test_tcp_egress_wraps_beyond_one_hop():
    _set_path(DEST, RELAY, 2)
    iface = _mk_tcp_iface()
    raw = _hdr1_linkrequest()
    assert iface.process_outgoing(raw) is True
    body = iface._socket.sent[1:-1]
    assert len(body) == len(raw) + 16
    assert (body[0] >> 6) & 0x01 == 1 and (body[0] >> 4) & 0x01 == 1
    assert body[2:18] == RELAY and body[18:34] == DEST


# ------------------------------- runner ----------------------------------
def _run():
    import traceback
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except Exception as e:
            failed += 1
            print("FAIL  " + name + "  ->  " + repr(e))
            traceback.print_exc()
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    return failed


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run() else 0)
