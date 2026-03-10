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
    "enable_transport": True,
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

        # ---- E220 LoRa (EByte E220-900T) ----
        # Transparent serial LoRa. Both nodes must share channel and air_rate.
        #
        # Wiring (ESP32-S3 example):
        #   E220 M0  -> GPIO4  (mode select)
        #   E220 M1  -> GPIO5  (mode select)
        #   E220 RXD -> GPIO17 (ESP32 TX)
        #   E220 TXD -> GPIO16 (ESP32 RX)
        #   E220 AUX -> GPIO6  (busy signal)
        #   E220 VCC -> 3.3V (22dBm) or 5V (30dBm)
        #   E220 GND -> GND
        #
        # auto_configure: sends AT commands at boot to set channel/rate/power.
        #   Set to False if pre-configured via USB-UART adapter.
        #
        # air_rate: 0-2 = 2.4kbps (max range ~5km LOS)
        #           3 = 4.8k, 4 = 9.6k, 5 = 19.2k, 6 = 38.4k, 7 = 62.5k
        #
        # tx_power: 0 = max, 1 = -4dB, 2 = -8dB, 3 = -12dB
        #
        # channel: freq = 850.125 + channel * 1MHz
        #   18 = 868.125 MHz (EU ISM), 72 = 922.125 MHz (US ISM)
        #
        # lbt: listen-before-talk, required in EU 868MHz.
        #
        # {
        #     "type": "E220Interface",
        #     "name": "LoRa E220",
        #     "enabled": True,
        #     "uart_id": 2,
        #     "tx_pin": 17,
        #     "rx_pin": 16,
        #     "speed": 9600,
        #     "m0_pin": 4,
        #     "m1_pin": 5,
        #     "aux_pin": 6,
        #     "auto_configure": True,
        #     "channel": 18,
        #     "air_rate": 2,
        #     "tx_power": 0,
        #     "lbt": True,
        # },

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
        {
            "type": "TCPClientInterface",
            "name": "VarnaTransport",
            "enabled": True,
            "target_host": "rn.varnatransport.com",
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
