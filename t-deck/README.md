# T-Deck LXMF Messenger

Standalone LoRa messaging device running on the LilyGO T-Deck v1.
Uses [uP-reticulum](../README.md) for Reticulum-compatible encrypted messaging over LoRa.

## Hardware

- **Board**: LilyGO T-Deck v1 (ESP32-S3)
- **Radio**: Semtech SX1262 LoRa transceiver (shared SPI bus with display)
- **Display**: ST7789 320x240 TFT (landscape)
- **Input**: QWERTY keyboard (I2C) + trackball with click button
- **Audio**: MAX98357A I2S amplifier — chirp on RX, blip on TX
- **Battery**: LiPo with ADC voltage monitoring

## Setup

### 1. Flash MicroPython

Flash MicroPython v1.22+ for ESP32-S3 to the T-Deck.

### 2. Install LoRa driver

```
mpremote mip install lora-sx126x
```

### 3. Upload files

Upload the `urns/` library and T-Deck files:

```
# Upload uP-reticulum library
mpremote cp -r urns/ :/lib/urns/

# Upload T-Deck app files
mpremote cp t-deck/tdeck_node.py t-deck/tdeck_config.py t-deck/ui.py :
mpremote cp t-deck/sound.py t-deck/st7789py.py t-deck/vga2_8x16.py :
```

### 4. Configure

Edit `tdeck_config.py`:

- `NODE_NAME` — display name broadcast in announces (default: `"T-Deck"`)
- `DEBUG` — `0` = silent, `1` = basic, `2` = verbose
- `LORA_CONFIG` — radio parameters (frequency, SF, BW, TX power, syncword)

Default radio settings: **868 MHz, SF7, BW125, CR5, 14 dBm, syncword 0x1424**.
These are compatible with RNode firmware and reference Reticulum.

### 5. Run

```python
import tdeck_node
```

Or set as `main.py` for autostart:

```
mpremote cp t-deck/tdeck_node.py :/main.py
```

## Usage

### Node List Screen

The device starts on the node list screen showing discovered peers.

| Action | Input |
|---|---|
| Select peer | Trackball up/down |
| Open chat | Trackball click or Enter |
| Send announce | Press `a` |

### Chat Screen

| Action | Input |
|---|---|
| Type message | Keyboard |
| Send message | Enter |
| Scroll history | Trackball up/down |
| Back to node list | Backspace (empty input) or Escape |

### Status Bar

Top bar shows: battery voltage, RSSI of last received packet, node name, and `[A]` flash on announce.

## Pin Map

| Function | Pin(s) |
|---|---|
| SPI SCK/MOSI | 40, 41 |
| Display CS/DC/BL | 12, 11, 42 |
| LoRa CS/RST/BUSY/DIO1/MISO | 9, 17, 13, 45, 38 |
| Keyboard SCL/SDA/PWR | 8, 18, 10 |
| Trackball U/D/L/R/Click | 3, 15, 1, 2, 0 |
| Speaker BCK/WS/DOUT | 7, 5, 6 |
| Battery ADC | 4 |

## Architecture

```
tdeck_node.py     Main entry: init hardware, wire LXMF callbacks to GUI
tdeck_config.py   Pin definitions, radio parameters, node name
ui.py             Async GUI: node list + chat screens, diff-based drawing
sound.py          I2S notification tones (RX chirp, TX blip)
st7789py.py       ST7789 display driver (pure Python)
vga2_8x16.py      8x16 VGA font
```

### SPI Bus Sharing

Display and LoRa share SPI1 (SCK=40, MOSI=41). Bus arbitration is CS-based only — display CS is deasserted during LoRa operations and vice versa. No SPI reinit at runtime.

### Display Optimization

The GUI uses diff-based drawing: a 15-row cache tracks what's currently on screen. Only changed rows trigger SPI writes, reducing traffic by ~80% on typical redraws. The trackball uses edge detection (HIGH-to-LOW transitions) to prevent noisy pins from flooding scroll events.

### SX1262 Notes

- **DC-DC regulator mode** is required for TX (`use_dcdc: True`). The driver defaults to LDO which produces no RF output on the T-Deck.
- **TCXO supply** must be set to 3.3V (`dio3_tcxo_millivolts: 3300`). Without it, modem init fails.

## Files

| File | Description |
|---|---|
| `tdeck_node.py` | Main app — hardware init, Reticulum/LXMF setup, async event loop |
| `tdeck_config.py` | All pin definitions and radio config in one place |
| `ui.py` | GUI state machine with cached drawing and async keyboard/trackball |
| `sound.py` | I2S tone generation for RX/TX notifications |
| `st7789py.py` | Pure Python ST7789 driver |
| `vga2_8x16.py` | Bitmap font (8x16 pixels per character, 40 columns) |
| `test.py` | Hardware test utilities |
