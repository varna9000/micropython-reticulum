# T-Deck v1 Configuration
# Pin definitions and LoRa parameters

NODE_NAME = "T-Deck"
DEBUG = 2

# --- Display (ST7789 on shared SPI1) ---
DISP_SCK  = 40
DISP_MOSI = 41
DISP_CS   = 12
DISP_DC   = 11
DISP_BL   = 42

# --- LoRa SX1262 (on shared SPI1) ---
LORA_SCK  = 40
LORA_MOSI = 41
LORA_MISO = 38
LORA_CS   = 9
LORA_RST  = 17
LORA_BUSY = 13
LORA_DIO1 = 45

# --- Keyboard (I2C) ---
KBD_SCL  = 8
KBD_SDA  = 18
KBD_PWR  = 10  # also peripheral power
KBD_INT  = 46
KBD_ADDR = 0x55

# --- Trackball ---
TB_UP    = 3
TB_DOWN  = 15
TB_LEFT  = 1
TB_RIGHT = 2
TB_CLICK = 0

# --- Battery ADC ---
BAT_PIN = 4

# --- Speaker (I2S MAX98357A) ---
SPK_BCK  = 7
SPK_WS   = 5
SPK_DOUT = 6

# --- LoRa radio config ---
LORA_CONFIG = {
    "type": "LoRaInterface",
    "name": "T-Deck LoRa",
    "enabled": True,
    "spi_bus": 1,
    "sck_pin": LORA_SCK,
    "mosi_pin": LORA_MOSI,
    "miso_pin": LORA_MISO,
    "cs_pin": LORA_CS,
    "busy_pin": LORA_BUSY,
    "dio1_pin": LORA_DIO1,
    "reset_pin": LORA_RST,
    "freq_khz": 868000,
    "sf": 7,
    "bw": "125",
    "coding_rate": 5,
    "tx_power": 14,
    "preamble_len": 8,
    "crc_en": True,
    "syncword": 0x1424,
    "dio2_rf_sw": True,
    "dio3_tcxo_millivolts": 3300,  # T-Deck SX1262 TCXO supply voltage
    "use_dcdc": True,  # DC-DC regulator mode (required for T-Deck TX)
    # spi, spi_acquire, spi_release injected at runtime by tdeck_node.py
}

# --- Reticulum config ---
CONFIG = {
    "loglevel": 3,
    "enable_transport": False,
    "interfaces": [LORA_CONFIG],
}
