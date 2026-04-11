"""
µReticulum — Node Configuration
================================
Edit this file to configure your node. Uncomment the interface(s) you need.
"""

# ---- Node settings ----
WIFI_SSID = "AP"
WIFI_PASS = "pass"
NODE_NAME = "ESP32s3"

# DEBUG levels: 0 = silent, 1 = messages & announces only, 2 = full debug
DEBUG = 2


# ---- Reticulum config ----
# All interfaces are listed below. Uncomment the ones you want to use.
# Multiple interfaces can be active at the same time (e.g. WiFi + LoRa).
CONFIG = {
    "loglevel": 3,
    "enable_transport": False,
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
         {
             "type": "E32Interface",
             "name": "LoRa E32",
             "enabled": True,
             "uart_id": 1,
             "tx_pin": 4,
             "rx_pin": 5,
             "speed": 9600,
             "m0_pin": 8,
             "m1_pin": 7,
             "aux_pin": 3,
             "auto_configure": False,
             "channel": 6,
             "air_rate": 2,
             "tx_power": 0,
         },

        # ---- SX1262 SPI LoRa (e.g. Seeed XIAO ESP32S3 + Wio-SX1262) ----
        # Native SPI LoRa using micropython-lib lora-sx126x driver.
        # Install: mpremote mip install lora-sx126x
        #
        # Kit version pins: CS=41, DIO1=39, RESET=42, BUSY=40
        # Header board pins: CS=5, DIO1=2, RESET=3, BUSY=4
        # SPI pins (both variants): SCK=7, MOSI=9, MISO=8
        #
        # freq_khz: 868000 (EU), 915000 (US), 923000 (AS)
        # sf: 7-12 (higher = longer range, slower)
        # bw: "125"/"250"/"500" (lower = longer range, slower)
        # tx_power: -9 to +22 dBm
        # syncword: 0x1424 (Reticulum/RNode compatible)
        # dio2_rf_sw: true = SX1262 internally drives DIO2 as RF switch (default, correct for Wio-SX1262)
        # dio3_tcxo_millivolts: 1800 for Wio-SX1262 TCXO. Set null to disable.
        #
        # {
        #     "type": "LoRaInterface",
        #     "name": "LoRa SX1262",
        #     "enabled": True,
        #     "spi_bus": 1,
        #     "sck_pin": 7,
        #     "mosi_pin": 9,
        #     "miso_pin": 8,
        #     "cs_pin": 41,
        #     "busy_pin": 40,
        #     "dio1_pin": 39,
        #     "reset_pin": 42,
        #     "freq_khz": 868000,
        #     "sf": 7,
        #     "bw": "125",
        #     "coding_rate": 5,
        #     "tx_power": 14,
        #     "preamble_len": 8,
        #     "crc_en": True,
        #     "syncword": 0x1424,
        #     "dio2_rf_sw": True,
        #     "dio3_tcxo_millivolts": 1800,
        # },

        # ---- TCP Client ----
        # Connects to a remote RNS TCP server (TCPServerInterface).
        # Uses HDLC framing, wire-compatible with reference Reticulum.
        #{
        #    "type": "TCPClientInterface",
        #    "name": "VarnaTransport",
        #    "enabled": True,
        #    "target_host": "rn.varnatransport.com",
        #    "target_port": 4243,
        #},

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
