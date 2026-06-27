# µReticulum Protocol Constants
# No internal imports - this module is the dependency root

from micropython import const

# Protocol MTU and sizes
MTU                   = const(500)
TRUNCATED_HASHLENGTH  = const(128)  # bits
IFAC_MIN_SIZE         = const(1)
IFAC_DEFAULT_SIZE     = const(16)   # bytes
IFAC_SALT             = b'\xad\xf5\x4d\x88\x2c\x9a\x9b\x80\x77\x1e\xb4\x99\x5d\x70\x2d\x4a\x3e\x73\x33\x91\xb2\xa0\xf5\x3f\x41\x6d\x9f\x90\x7e\x55\xcf\xf8'
HEADER_MINSIZE        = const(2 + 1 + (128 // 8))       # 19
HEADER_MAXSIZE        = const(2 + 1 + (128 // 8) * 2)   # 35
MDU                   = const(500 - 35 - 1)              # 464
DEFAULT_PER_HOP_TIMEOUT = const(6)

# Identity / Key sizes (bits)
KEYSIZE               = const(512)    # 256 enc + 256 sig
RATCHETSIZE           = const(256)
HASHLENGTH            = const(256)
SIGLENGTH             = const(512)    # = KEYSIZE
NAME_HASH_LENGTH      = const(80)
AES128_BLOCKSIZE      = const(16)     # bytes
TOKEN_OVERHEAD        = const(48)     # bytes
DERIVED_KEY_LENGTH    = const(64)     # 512//8

# Destination types
DEST_SINGLE           = const(0x00)
DEST_GROUP            = const(0x01)
DEST_PLAIN            = const(0x02)
DEST_LINK             = const(0x03)

# Destination directions
DIR_IN                = const(0x11)
DIR_OUT               = const(0x12)

# Proof strategies
PROVE_NONE            = const(0x21)
PROVE_APP             = const(0x22)
PROVE_ALL             = const(0x23)

# Packet types
PKT_DATA              = const(0x00)
PKT_ANNOUNCE          = const(0x01)
PKT_LINKREQUEST       = const(0x02)
PKT_PROOF             = const(0x03)

# Header types
HDR_1                 = const(0x00)
HDR_2                 = const(0x01)

# Packet contexts
CTX_NONE              = const(0x00)
CTX_RESOURCE          = const(0x01)
CTX_RESOURCE_ADV      = const(0x02)
CTX_RESOURCE_REQ      = const(0x03)
CTX_RESOURCE_HMU      = const(0x04)
CTX_RESOURCE_PRF      = const(0x05)
CTX_RESOURCE_ICL      = const(0x06)
CTX_RESOURCE_RCL      = const(0x07)
CTX_CACHE_REQUEST     = const(0x08)
CTX_REQUEST           = const(0x09)
CTX_RESPONSE          = const(0x0A)
CTX_PATH_RESPONSE     = const(0x0B)
CTX_COMMAND           = const(0x0C)
CTX_COMMAND_STATUS    = const(0x0D)
CTX_CHANNEL           = const(0x0E)
CTX_KEEPALIVE         = const(0xFA)
CTX_LINKIDENTIFY      = const(0xFB)
CTX_LINKCLOSE         = const(0xFC)
CTX_LINKPROOF         = const(0xFD)
CTX_LRRTT             = const(0xFE)
CTX_LRPROOF           = const(0xFF)

# Context flags
FLAG_SET              = const(0x01)
FLAG_UNSET            = const(0x00)

# Transport types
TRANSPORT_BROADCAST   = const(0x00)
TRANSPORT_TRANSPORT   = const(0x01)
TRANSPORT_RELAY       = const(0x02)
TRANSPORT_TUNNEL      = const(0x03)

# Link constants
LINK_CURVE            = "Curve25519"
LINK_ECPUBSIZE        = const(32)  # bytes

# Table size limits (microcontroller caps)
MAX_DESTINATIONS      = const(64)
MAX_PATH_TABLE        = const(32)
MAX_ACTIVE_LINKS      = const(4)
MAX_ANNOUNCE_QUEUE    = const(16)
MAX_RECEIPTS          = const(32)
MAX_INCOMING_RESOURCES = const(1)
MAX_OUTGOING_RESOURCES = const(2)
TRANSPORT_HOPLIMIT    = const(128)

# Timing
RATCHET_EXPIRY        = const(60 * 60 * 24 * 30)  # 30 days
RATCHET_COUNT         = const(512)
RATCHET_INTERVAL      = const(30 * 60)  # 30 min

# Encrypted MDU calculation
import math as _math
ENCRYPTED_MDU = _math.floor((MDU - TOKEN_OVERHEAD - KEYSIZE // 16) / AES128_BLOCKSIZE) * AES128_BLOCKSIZE - 1
PLAIN_MDU = MDU

# ---------------------------------------------------------------------------
# Transport routing / relay (directed forwarding) — see firmware transport plan
# ---------------------------------------------------------------------------

# Pathfinder parameters (subset of reference RNS Transport)
PATHFINDER_R           = const(1)          # announce retransmit retries
PATHFINDER_G           = const(5)          # retry grace period (s)
PATHFINDER_RW          = 0.5               # random rebroadcast window (s) — float
LOCAL_REBROADCASTS_MAX = const(2)          # back off after N neighbour rebroadcasts
ANNOUNCE_CAP           = 0.02              # max fraction of interface airtime for announces
MAX_HOPS               = const(16)         # mesh hop limit (reference RNS uses 128)

# Routing-table expiry (seconds)
PATH_EXPIRY            = const(60 * 60 * 24)    # 1 day
REVERSE_TIMEOUT       = const(8 * 60)          # 8 minutes
LINK_ENTRY_TIMEOUT    = const(15 * 60)         # link-table entry lifetime

# Routing-table caps (RAM-bounded for MCU)
MAX_REVERSE_TABLE     = const(32)
MAX_LINK_TABLE        = const(16)
MAX_ANNOUNCE_TABLE    = const(16)
MAX_PACKET_CACHE      = const(32)
MAX_PACKET_HASHLIST   = const(512)

# Maintenance / flood-control (Phase 5/6)
CULL_INTERVAL         = const(5)           # seconds between table-maintenance passes
PERSIST_INTERVAL      = const(300)         # seconds between path-table flash writes
ANNOUNCE_RATE_WINDOW  = const(60)          # per-source announce rate window (s)
ANNOUNCE_RATE_MAX     = const(6)           # max announces/source/window before throttling rebroadcast

# path_table entry: [timestamp, next_hop, hops, expires, recv_if, announce_hash, emitted]
IDX_PT_TIMESTAMP      = const(0)
IDX_PT_NEXT_HOP       = const(1)
IDX_PT_HOPS           = const(2)
IDX_PT_EXPIRES        = const(3)
IDX_PT_RECV_IF        = const(4)
IDX_PT_ANNOUNCE       = const(5)
IDX_PT_EMITTED        = const(6)

# reverse_table entry: [recv_if, outbound_if, timestamp]
IDX_RT_RECV_IF        = const(0)
IDX_RT_OUTB_IF        = const(1)
IDX_RT_TIMESTAMP      = const(2)

# link_table entry: [ts, next_hop, nh_if, rem_hops, recv_if, hops, dest, validated, proof_tmo]
IDX_LT_TIMESTAMP      = const(0)
IDX_LT_NEXT_HOP       = const(1)
IDX_LT_NH_IF          = const(2)
IDX_LT_REM_HOPS       = const(3)
IDX_LT_RECV_IF        = const(4)
IDX_LT_HOPS           = const(5)
IDX_LT_DEST           = const(6)
IDX_LT_VALIDATED      = const(7)
IDX_LT_PROOF_TMO      = const(8)

# announce_table entry: [ts, retransmit_tmo, retries, recv_from, hops, raw, lcl_rbrd, blk_rbrd, attchd_if]
IDX_AT_TIMESTAMP      = const(0)
IDX_AT_RTMO           = const(1)
IDX_AT_RETRIES        = const(2)
IDX_AT_RECV_FROM      = const(3)
IDX_AT_HOPS           = const(4)
IDX_AT_RAW            = const(5)
IDX_AT_LCL_RBRD       = const(6)
IDX_AT_BLK_RBRD       = const(7)
IDX_AT_ATTCHD_IF      = const(8)
