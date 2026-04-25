# ESP32-S3 Camera Board

- **Source:** <https://www.aliexpress.com/item/1005008285512156.html>
- **MicroPython camera API:** <https://github.com/cnadler86/micropython-camera-API>

## Specifications

| Category | Details |
|---|---|
| **Controller** | Xtensa Dual-Core 32-Bit LX7 CPU, up to 240 MHz |
| **Storage** | 384 KB ROM, 512 KB SRAM, 16 KB RTC SRAM, 8 MB PSRAM |
| **Operating Voltage** | 3V -- 3.6V |
| **Wireless** | Wi-Fi (IEEE 802.11 b/g/n, 2.4 GHz, 150 Mbps max); Bluetooth LE 5.0 |
| **Camera Support** | OV2640 / OV5640 / OV3660 (optional) |
| **Temperature Range** | -40 C to 65 C |
| **Dimensions** | 57 mm x 28 mm |
| **Antenna** | PCB onboard |

## Peripherals

- 45 GPIOs
- 2x 12-bit SAR ADC (up to 20 channels)
- 4x SPI, 3x UART, 2x I2C, 2x I2S
- 1x USB OTG, 1x USB Serial/JTAG
- 1x LCD interface (8/16-bit parallel RGB, 8080, MOTO6800)
- 1x DVP camera interface (8--16 bit)
- 1x RMT (TX/RX), 1x pulse counter
- LED PWM controller (up to 8 channels)
- 2x MCPWM
- 1x SDIO host (2 card slots)
- General DMA controller (5 RX / 5 TX channels)
- 1x TWAI controller (CAN 2.0 compatible)
- 14x capacitive sensing GPIOs
- 1x temperature sensor

## Timers

- 4x 54-bit general timers
- 1x 52-bit system timer
- 3x watchdog timers

## Wi-Fi

- IEEE 802.11 b/g/n, 20/40 MHz bandwidth, up to 150 Mbps (1T1R)
- WMM, frame aggregation (A-MPDU, A-MSDU), immediate block ACK
- Infrastructure BSS Station, SoftAP, and Station+SoftAP modes
- 4x virtual Wi-Fi interfaces
- Antenna diversity, 802.11mc FTM
- External power amplifier support

## Bluetooth

- Bluetooth LE 5.0 + Mesh
- High power mode (20 dBm, shared PA with Wi-Fi)
- Data rates: 125 Kbps, 500 Kbps, 1 Mbps, 2 Mbps
- Advertising extensions, multiple advertisement sets
- Channel selection algorithm #2
- Wi-Fi and Bluetooth coexistence on shared antenna
