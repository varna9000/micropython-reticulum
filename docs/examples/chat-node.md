# Chat node (`example_node.py`)

The default example. Receives LXMF messages, echoes them back, and can drive the onboard NeoPixel (GPIO 21 on the Waveshare S3-Zero) from chat commands. Needs any ESP32-S3 with WiFi and standard MicroPython.

| You send | What happens |
|----------|--------------|
| `red` / `green` / `blue` | Onboard LED changes colour |
| `off` | LED turns off |
| anything else | Echoed back to you |

Commands are case-insensitive. The same pattern can drive relays, motors, or any GPIO-attached hardware — see [Peripherals](../../README.md#peripherals).

[← back to the README](../../README.md#what-can-i-build-with-this)
