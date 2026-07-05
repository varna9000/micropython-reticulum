"""
µReticulum — Node Configuration
================================
Edit this file to configure your node. Uncomment the interface(s) you need.
"""

from lora_boards import LORA_BOARDS

# ---- Node settings ----
WIFI_SSID = "YOUR_WIFI_SSID"      # <- your WiFi network name
WIFI_PASS = "YOUR_WIFI_PASSWORD"  # <- your WiFi password
NODE_NAME = "ESP32s3"

# WebREPL login password (4-9 chars) for over-the-network control at
# ws://<node-ip>:8266/  (started by boot.py). Change it from the default.
WEBREPL_PASSWORD = "changeme"

# DEBUG levels: 0 = silent, 1 = messages & announces only, 2 = full debug
DEBUG = 2


# ---- Reticulum config ----
# All interfaces are listed below. Uncomment the ones you want to use.
# Multiple interfaces can be active at the same time (e.g. WiFi + LoRa).
CONFIG = {
    "loglevel": 3,
    # True = this node RELAYS for others (a transport router); False = leaf node.
    # The example you run decides the role: example_transport_router = pure relay,
    # example_node = LXMF inbox (and also relays if this is True).
    "enable_transport": True,

    # LoRa board pinout presets (see firmware/lora_boards.py). An interface
    # below references one with "board": "<name>"; the preset's pins are merged
    # in at startup. Edit lora_boards.py to add a board — no other changes.
    "lora_boards": LORA_BOARDS,

    # Dedicated destination that replies to `rnprobe` (reference RNS tool).
    # Set "enabled": True to expose the probe destination. See README.
    "probe": {
        "enabled": False,
        "app_name": "urns",            # full_name = "urns.probe"
        "aspect": "probe",
        "announce_interval": 60 * 60,  # 1 hour; 0 = announce once at boot only
    },

    # ---- Time sync ----
    # Pure-LoRa nodes have no RTC or NTP, so their clock sits at 2000-01-01 and
    # every outgoing message/announce is stamped "Jan 2000". With time sync
    # enabled, the node learns the real time ONCE per boot from a trusted
    # peer it overhears — either an announce timestamp or a signature-validated
    # LXMF message timestamp. After that the ESP32's internal RTC keeps time
    # for the rest of the power-on session (a reboot resets it, then it
    # re-syncs from the next trusted packet).
    #
    # Two modes:
    #   - Authority: list LXMF delivery destination hashes (hex, exactly as
    #     shown in MeshChat/Sideband) in trusted_nodes. One matching source
    #     sets the clock immediately.
    #   - Corroboration: leave trusted_nodes empty. The clock is set only once
    #     `min_sources` distinct peers agree on the time within `tolerance`
    #     seconds (the median is applied). No single node can set it alone.
    "time_sync": {
        "enabled": True,
        "trusted_nodes": [
            # "a1b2c3d4e5f60718293a4b5c6d7e8f90",   # e.g. your phone's MeshChat address
        ],
        "min_sources": 2,      # corroboration quorum when trusted_nodes is empty
        "tolerance": 120,      # seconds; max clock disagreement between peers
    },

    "interfaces": [

        # ---- WiFi UDP ----
        # Broadcasts on the local LAN. Works with MeshChat / Sideband.
        # Set forward_ip to None for auto-detected subnet broadcast.
         # {
         #     "type": "UDPInterface",
         #     "name": "WiFi UDP",
         #     "enabled": True,
         #     "listen_ip": "0.0.0.0",
         #     "listen_port": 4242,
         #     "forward_ip": "255.255.255.255",
         #     "forward_port": 4242,
         # },

        # ---- E32 LoRa (EByte E32-900T20) ----
        # Transparent serial LoRa with hex register config.
        # Both nodes must share channel and air_rate.
        #
        # Wiring (RP2040 Zero example):
        #   E32 M0  -> GPIO8  (mode select)
        #   E32 M1  -> GPIO7  (mode select)
        #   E32 TXD -> GPIO5  (MCU RX)
        #   E32 RXD -> GPIO4  (MCU TX)
        #   E32 AUX -> GPIO3  (busy signal)
        #   E32 VCC -> 3.3V
        #   E32 GND -> GND
        #
        # auto_configure: writes 6-byte hex register at boot to set
        #   channel/rate/power. Set False if pre-configured via USB adapter.
        #
        # air_rate: 0 = 300bps, 1 = 1200, 2 = 2400 (default, max range)
        #           3 = 4800, 4 = 9600, 5-7 = 19200
        #
        # tx_power: 0 = 20dBm, 1 = 17dBm, 2 = 14dBm, 3 = 10dBm
        #
        # channel: freq = 862 + channel * 1MHz (E32-900T)
        #   6 = 868 MHz (EU ISM), 60 = 922 MHz (US ISM)
        #
         # {
         #     "type": "E32Interface",
         #     "name": "LoRa E32",
         #     "enabled": True,
         #     "uart_id": 1,
         #     "tx_pin": 4,
         #     "rx_pin": 5,
         #     "speed": 9600,
         #     "m0_pin": 15,
         #     "m1_pin": 2,
         #     "aux_pin": 6,
         #     "auto_configure": False,
         #     "timeout": 3000,
         #     "channel": 6,
         #     "air_rate": 2,
         #     "tx_power": 3,
         # },

        # ---- SX1262 SPI LoRa (micropython-lib lora-sx126x driver) ----
        # Install: mpremote mip install lora-sx126x
        #
        # The board's pins come from a preset in lora_boards.py — just set
        # "board" to its name. Only network/radio params live here, and they
        # must match on every node of the mesh:
        #   freq_khz: 868000 (EU), 915000 (US), 923000 (AS)
        #   sf: 7-12 (higher = longer range, slower)
        #   bw: "125"/"250"/"500" (lower = longer range, slower)
        #   tx_power: -9 to +22 dBm     syncword: 0x1424 (Reticulum/RNode)
        #   lbt_rssi: CSMA/listen-before-talk busy threshold in dBm; TX defers
        #             while channel RSSI >= this (default -100, None disables)
        #   lbt_max_ms: max LBT wait before transmitting anyway (default 2000)
        #
        # Available board presets (see lora_boards.py):
        #   "xiao_esp32s3_sx1262"          XIAO ESP32-S3 + Wio-SX1262 (kit)
        #   "xiao_esp32s3_sx1262_header"   XIAO ESP32-S3 + Wio-SX1262 (header)
        #   "esp32s3_cam_sx1262"           ESP32-S3 WROOM CAM + Wio-SX1262
        #   "HTIT-WB32LAF"                 Heltec V4 LoRa HTIT-WB32LAF + SX-1262 embedded
        #
        # Any pin can still be overridden inline (it wins over the preset).
        #
        {
            "type": "LoRaInterface",
            "board": "xiao_esp32s3_sx1262",   # XIAO ESP32-S3 + Wio-SX1262 kit
            "name": "LoRa",
            "enabled": True,
            "freq_khz": 868800,
            "sf": 8,
            "bw": "125",
            "coding_rate": 5,
            "tx_power": 22,
            "preamble_len": 8,
            "crc_en": True,
            "syncword": 0x1424,
        },

        # ---- TCP Client (the WiFi/IP side of a transport bridge) ----
        # Connects OUT to a remote RNS TCPServerInterface (rnsd / MeshChat on the
        # LAN, or a public transport node). HDLC-framed, wire-compatible with RNS.
        {
            "type": "TCPClientInterface",
            "name": "WiFi TCP",
            "enabled": True,
            "target_host": "192.168.1.10",   # <- your RNS TCP server IP / hostname
            "target_port": 4243,
        },

        # ---- Serial (for RNode / wired link) ----
        # {
        #     "type": "SerialInterface",
        #     "name": "Serial Link",
        #     "enabled": True,
        #     "uart_id": 1,
        #     "tx_pin": 17,
        #     "rx_pin": 16,
        #     "speed": 115200,
        # },

    ],
}

# ---- Sensor Network config ----
# LXMF Destination address for data to be sent to
SENSOR_HUB = ""
