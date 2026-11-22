# Interfaces reference

Full option reference for the interface and node-level features that the [README](../README.md#interfaces) only summarises: SX1262 radio parameters and board presets, E32 parameters, wiring and gotchas, IFAC sizing, transport (relay) mode internals, the rnprobe responder and mesh time sync.

## SX1262 radio parameters and board presets

Board pinouts live in `firmware/lora_boards.py` as named presets, referenced from an interface entry with `"board": "<name>"`. The pins are merged in at startup. Any pin set explicitly on the interface overrides the preset, so you can tweak one pin without editing `lora_boards.py`.

**Built-in presets:**

| `board` | Hardware |
|---------|----------|
| `xiao_esp32s3_sx1262` | Seeed XIAO ESP32-S3 + Wio-SX1262 (kit) |
| `xiao_esp32s3_sx1262_header` | XIAO ESP32-S3 + Wio-SX1262 (header board) |
| `esp32s3_cam_sx1262` | ESP32-S3 WROOM CAM module + Wio-SX1262 |

**Adding a board:** add one entry to `LORA_BOARDS` in `firmware/lora_boards.py` with that board's `sck/mosi/miso/cs/busy/dio1/reset` pins (plus `dio2_rf_sw`, `dio3_tcxo_millivolts`, and optionally `use_dcdc` / `spi_baudrate`), then point an interface at it by name. Radio params stay in `config.py` so every node on the mesh shares them.

**Radio parameters** (interface entry — must match across the whole mesh)

- `freq_khz`: 868000 (EU), 915000 (US), 923000 (AS).
- `sf`: 7–12 (higher = longer range, slower).
- `bw`: `"125"` / `"250"` / `"500"` (lower = longer range, slower).
- `tx_power`: -9 to +22 dBm.
- `syncword`: `0x1424` — Reticulum/RNode-compatible.
- `dio2_rf_sw`: `True` on Wio-SX1262 (radio drives DIO2 as RF switch internally).
- `dio3_tcxo_millivolts`: `1800` on Wio-SX1262 (TCXO). `None` to disable (crystal-only modules).
- `lbt_rssi`: CSMA/listen-before-talk busy threshold in dBm (default `-100`, `None` disables). Every frame TX first probes the channel and defers in short random slots while it's busy — same etiquette as RNode firmware, essential when a repeater shares the channel.
- `lbt_max_ms`: max LBT wait before transmitting anyway (default `2000`).

The receive path is also hardened against **transparent repeaters** (devices that re-transmit every frame verbatim): duplicate halves of split packets (>254 B) are detected and dropped instead of corrupting reassembly.

## E32 parameters, wiring and gotchas

**Parameters**

- `channel`: freq = 862 + channel MHz. Channel 6 = 868 MHz (EU ISM), 60 = 922 MHz (US ISM).
- `air_rate`: 0 = 300 bps, 1 = 1200, 2 = 2400 (default), 3 = 4800, 4 = 9600, 5 = 19200.
- `tx_power`: 0 = 20 dBm, 1 = 17 dBm, 2 = 14 dBm, 3 = 10 dBm.
- `auto_configure`: `True` writes the channel/rate/power registers to the module's flash at boot. Set `False` once the module is configured.
- `timeout`: HDLC frame timeout in ms. Must be >2× the air time of a full packet. At 2400 bps a 182-byte announce takes ~760 ms, so 3000 ms is safe.

**Wiring (Waveshare RP2040-Zero example)**

| E32 Pin | Function | RP2040 GPIO |
|---------|----------|-------------|
| RXD | Module RX | GPIO 4 (UART1 TX) |
| TXD | Module TX | GPIO 5 (UART1 RX) |
| M0 | Mode select | GPIO 15 |
| M1 | Mode select | GPIO 2 |
| AUX | Busy signal | GPIO 6 |
| VCC | Power | 5 V |
| GND | Ground | GND |

**Pin gotcha:** on RP2040, do **not** use UART1 alternate-function pins (GPIO 3, 6, 7, 8) for M0/M1 — UART1 init claims them for CTS/RTS/TX and the resulting contention can damage the GPIO drivers. The driver also sets M0/M1 to 12 mA drive strength (vs the 4 mA default) so the E32's internal pull-ups release reliably.

**Power gotcha:** the E32-900T20D draws ~120 mA at 20 dBm TX. On RP2040-Zero this current spike will crash the MCU even off the 5 V USB rail. Use `tx_power: 3` (10 dBm, ~40 mA) unless the E32 has its own supply with decoupling.

## IFAC size

The optional `ifac_size` is in **bytes** (the reference config takes bits: `ifac_size = 128` there is `16` here) and must match the other end. The default is 16, except on `LoRaInterface` where it is 8: a full 500 B packet plus the code has to fit the RNode two-frame limit of 508 B, so the LoRa interface also clamps a larger value to 8 and logs it. An empty string counts as unset, and calling `setup_ifac()` again with neither key clears a code set earlier.

## Transport mode (full routing — turn a node into a relay)

Set `enable_transport: True` and the node becomes a **Reticulum transport router**: it forwards traffic between its interfaces so a LoRa-only mesh reaches the wider network and back. It is wire-compatible with reference RNS — a µReticulum router can sit transparently in a path between reference RNS, MeshChat, Sideband or NomadNet nodes.

This is **directed routing, not blind flooding**: the node learns routes from announces and forwards each packet on the *one* correct interface toward its destination, extending range without saturating the mesh. [`example_transport_router.py`](../firmware/example_transport_router.py) is a ready-made LoRa ↔ WiFi/TCP router built on it.

```python
CONFIG = {
    "enable_transport": True,                   # this node relays for others
    "interfaces": [
        { "type": "LoRaInterface",      ... },  # the LoRa mesh side
        { "type": "TCPClientInterface", ... },  # the IP side (rnsd / MeshChat); or a UDPInterface
    ],
}
```

**What it carries** — everything, multi-hop, wire-compatible:

| Traffic | How the router handles it |
|---|---|
| **Announces** | re-broadcast with the router's transport id stamped in (so downstream nodes learn the route back), with jitter, retries, neighbour-suppression and per-source rate-limiting |
| **Opportunistic messages** (single-packet LXMF) | directed forward to the next-hop interface via the path table; the delivery **proof** returns along the recorded reverse path |
| **Link sessions** (MeshChat / Sideband / NomadNet) | a link table is built from the transit `LINKREQUEST`; in-link traffic is routed both ways, the link proof returns, and the link **MTU is clamped** at the LoRa↔IP boundary so packets still fit on the air |
| **Resource transfers** (large messages, ≤ 16 KB) | ride the link table automatically |
| **Path requests** | answered on demand by replaying the cached announce; unknown routes trigger recursive discovery |

Routing state lives in RAM-bounded tables: `path_table` (dest → next-hop + interface + hop count), `reverse_table` (proof return), `link_table` (link/resource transit), plus a small cache of recent announces.

**Choosing between equal paths** — when the same announce reaches a node over two interfaces at the same hop count, whichever copy arrived first would otherwise keep the path forever. Set `gravity` on an interface to express a preference (RNS 1.4.1 semantics: higher wins, `0` is neutral, negatives discouraged). `default_gravity` at the top level applies to every interface that does not set its own.

```python
CONFIG = {
    "default_gravity": 0,
    "interfaces": [
        { "type": "TCPClientInterface", "gravity": 5,  ... },   # prefer IP when both work
        { "type": "LoRaInterface",      "gravity": 0,  ... },   # fall back to radio
    ],
}
```

Gravity only breaks ties. It never buys a longer path — a shorter route always wins first, regardless of preference.

**Resilience** (built for an open, long-running mesh): routing tables expire and are **purged when an interface drops** (WiFi-flap recovery); per-source announce rate-limiting and hard table caps prevent runaway memory; optional **strict link-proof validation** (native-gated Ed25519, ~17 ms); **blackholing** of misbehaving identities; and the **path table persists to flash** so a reboot isn't a mesh blackout.

**Watching it work**: every forward logs a `Relay …` line at `NOTICE` and bumps a counter, so you can follow relay activity in the console. The router example also serves a plain-HTTP dashboard on the LAN ([`webmonitor.py`](../firmware/webmonitor.py)) showing live `RELAYED ann/data/link/proof` counts, the path table, and the log stream. Path-table rows are labeled with the peer's announced display name **and the protocol behind each destination hash** — `lxmf` (messaging peer), `lxmf-pn` (propagation node), `nomad` (NomadNet pages), `voice-lxst` / `voice-mc` (LXST and MeshChat call endpoints), `probe`, or a `?hex` tag for unknown apps. Classification reads the `name_hash` every announce carries (no decryption involved) and survives reboots by recomputing labels from persisted identities.

**Running it headless**: a transport router usually runs without a USB cable, so it wants WiFi up at boot and a way back in to control it. [`boot.py`](../firmware/boot.py) can bring up **WiFi + WebREPL** automatically on every reset — but it ships **commented out**, so a plain leaf node (LoRa-only, sensor, proxy) boots straight to the REPL instead of sitting through a needless ~15 s WiFi connect. Uncomment the execution block at the bottom of `boot.py` **only on a transport node**; you can then reach it at `ws://<node-ip>:8266/` (log in with `WEBREPL_PASSWORD` from `config.py`) to start/stop the router and push fixes over the air.

> A transport router wants the RAM headroom of an ESP32-S3 (PSRAM is ideal). Forwarding is single-instance (no shared-instance or tunnel interfaces) — most useful as a **LoRa ↔ IP gateway**.

## Probe responder (rnprobe)

Expose a dedicated destination that replies to `rnprobe`, the reference reachability/RTT tool. Useful for debugging transport paths.

```python
CONFIG = {
    "probe": {
        "enabled": True,
        "app_name": "urns",            # full_name = "urns.probe"
        "aspect": "probe",
        "announce_interval": 60 * 60,  # 1 hour; 0 = announce once at boot only
    },
    "interfaces": [...],
}
```

When enabled the boot log prints the destination hash and full name:

```
Probe address: 4a1b… (urns.probe)
```

From a desktop with reference RNS installed:

```bash
rnprobe urns.probe 4a1b…
```

Both `full_name` and `destination_hash` are required: announces only carry a hash of the name, so the dot-name has to be known out of band. A successful probe prints `Valid reply received from <hash>` with the measured RTT. The probe destination refuses link requests — it only signs PROOF replies. Other apps filter it out of their UIs by app_name.

## Time sync (clock for pure-LoRa nodes)

A LoRa-only node has no WiFi/NTP and no battery-backed RTC, so its clock sits at `2000-01-01` and every message/announce it sends is stamped *January 2000* (you'll see this on received images in MeshChat). Time sync fixes this by learning the real time **from the mesh itself** — every announce and every signed LXMF message already carries the sender's Unix timestamp.

```python
CONFIG = {
    "time_sync": {
        "enabled": True,
        "trusted_nodes": [],   # see modes below
        "min_sources": 2,      # corroboration quorum (when trusted_nodes is empty)
        "tolerance": 120,      # seconds of allowed disagreement between peers
    },
    "interfaces": [...],
}
```

Two modes:

- **Authority** — list one or more LXMF delivery hashes (hex, exactly as shown in MeshChat/Sideband) in `trusted_nodes`. The first announce or signed message from a matching node sets the clock. Fastest, and corrects time on the very first packet heard.
- **Corroboration** — leave `trusted_nodes` empty. The clock is set only once `min_sources` *distinct* peers agree on the time within `tolerance` seconds (the median is applied). No single node can move your clock, so you don't have to trust anyone in particular.

The sync runs **once per power-on**, only while the clock is still unset — it never re-adjusts mid-session. A reboot resets the RTC to 2000, and the node re-syncs from the next qualifying packet. After syncing, both outgoing message timestamps and announce timestamps are correct for the rest of the session.

The moment the clock syncs, the node **automatically re-announces** all its destinations: announces sent before sync carry a year-2000 emission timestamp and are rejected as stale replays by peers that knew the node from a previous boot — without the re-announce, a rebooted node would stay invisible to the mesh until its next periodic announce.

> The ESP32's internal RTC keeps time only while powered — it does not survive a full power cycle. For instant-correct time at boot with no peer audible, add a battery-backed RTC (e.g. DS3231 over I²C).
