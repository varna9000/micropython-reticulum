"""
µReticulum — LoRa board pinout presets
======================================
Hardware-fixed pinouts for SX126x SPI LoRa boards, defined once and referenced
by name from config.py:

    {
        "type": "LoRaInterface",
        "board": "esp32s3_cam_sx1262",   # pinout preset (this file)
        "name": "LoRa",
        "enabled": True,
        # network/radio params live in config.py, shared across all nodes:
        "freq_khz": 868800, "sf": 8, "bw": "125", "coding_rate": 5,
        "tx_power": 14, "preamble_len": 8, "crc_en": True, "syncword": 0x1424,
    }

A preset holds ONLY values that are fixed by the board's wiring:
  spi_bus, sck_pin, mosi_pin, miso_pin, cs_pin, busy_pin, dio1_pin, reset_pin,
  dio2_rf_sw, dio3_tcxo_millivolts, and optionally use_dcdc / spi_baudrate.

Radio/network parameters (freq, sf, bw, coding_rate, tx_power, syncword, …) are
NOT board-specific — they must match across every node on the mesh — so they
stay in the interface entry in config.py.

To add a board: copy a block below, rename the key, and set its pins. Any key
you also set in the interface entry overrides the preset (handy for one-off
tweaks without editing this file).
"""

LORA_BOARDS = {

    # Seeed XIAO ESP32-S3 + Wio-SX1262 (kit version).
    "xiao_esp32s3_sx1262": {
        "spi_bus": 1,
        "sck_pin": 7,
        "mosi_pin": 9,
        "miso_pin": 8,
        "cs_pin": 41,
        "busy_pin": 40,
        "dio1_pin": 39,
        "reset_pin": 42,
        "dio2_rf_sw": True,
        "dio3_tcxo_millivolts": 1800,
    },

    # Seeed XIAO ESP32-S3 + Wio-SX1262 (header board variant).
    # Same SPI bus/pins as the kit; only the control pins differ.
    "xiao_esp32s3_sx1262_header": {
        "spi_bus": 1,
        "sck_pin": 7,
        "mosi_pin": 9,
        "miso_pin": 8,
        "cs_pin": 5,
        "busy_pin": 4,
        "dio1_pin": 2,
        "reset_pin": 3,
        "dio2_rf_sw": True,
        "dio3_tcxo_millivolts": 1800,
    },

    # ESP32-S3 WROOM N16R8 CAM module wired to a Wio-SX1262 (XIAO variant).
    # Reuses the SD-MMC pins (GPIO38/39/40) for SPI — do NOT mount the SD card
    # slot while LoRa is active. RESET (GPIO45) is a strapping pin.
    "esp32s3_cam_sx1262": {
        "spi_bus": 1,
        "sck_pin": 39,
        "mosi_pin": 38,
        "miso_pin": 40,   # shared with SD CLK/CMD/D1
        "cs_pin": 47,
        "busy_pin": 41,
        "dio1_pin": 42,
        "reset_pin": 45,
        "dio2_rf_sw": True,
        "dio3_tcxo_millivolts": 1800,
    },

    # LilyGO T-Deck v1 (ESP32-S3 + SX1262). Needs DC-DC regulator mode, a 3v3
    # TCXO, and runs SPI at 8 MHz. The radio shares SPI1 with the display, so a
    # LoRa-only T-Deck can use this preset directly; if you also drive the
    # display you must build the shared machine.SPI object yourself and pass it
    # in via the "spi"/"spi_acquire"/"spi_release" interface keys (bus
    # arbitration), which can't be expressed as a static preset.
    #
    # TODO: fill in dio1_pin / busy_pin / reset_pin / miso_pin from your working
    # T-Deck radio.py — they are not documented in this repo.
    # "tdeck_v1_sx1262": {
    #     "spi_bus": 1,
    #     "sck_pin": 40,
    #     "mosi_pin": 41, "miso_pin": None,
    #     "cs_pin": 9,
    #     "busy_pin": None,
    #     "dio1_pin": None,
    #     "reset_pin": None,
    #     "dio2_rf_sw": True,
    #     "dio3_tcxo_millivolts": 3300,
    #     "use_dcdc": True,
    #     "spi_baudrate": 8_000_000,
    # },

}
