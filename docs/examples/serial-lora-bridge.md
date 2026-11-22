# USB serial ↔ LoRa chat bridge (`example_proxy.py`)

Turns an RP2040 (e.g. Waveshare RP2040-Zero) with a LoRa radio (E32 or SX1262) into a USB-to-Reticulum bridge: chat over LoRa from any laptop without MeshChat. Enable a LoRa interface in `CONFIG["interfaces"]`, plug the board in and open a serial terminal (`screen`, `minicom`, `tio`, `PuTTY`) on its USB CDC port.

| Command | Effect |
|---------|--------|
| `/help` | Show available commands |
| `/peers` | List known peers |
| `/to <hex>` | Set the chat target by hex-hash prefix |
| `/me` / `/name` | Show this node's LXMF address / display name |
| `/announce` | Broadcast identity now |
| `/quit` | Return to the MicroPython REPL |

Text not starting with `/` is sent to the current target, which auto-switches to the most recent peer who messaged you.

[← back to the README](../../README.md#what-can-i-build-with-this)
