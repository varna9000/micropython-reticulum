# E-ink status node: bytecode deployment

The Heltec Wireless Paper has no PSRAM (about 220 KB of heap), so the stack is deployed as precompiled bytecode with the native Ed25519 module left off the device. This is the full staging and upload script referenced from the [README](../README.md#6--e-ink-status-node-example_eink_nodepy-heltec-wireless-paper).

```bash
MPY=mpy-cross   # from the MicroPython repo, same major version as the board
mkdir -p stage/lib/lora
for f in $(cd firmware && find urns peripherals -name '*.py' -not -path '*__pycache__*'); do
  mkdir -p stage/$(dirname $f); $MPY -march=xtensawin -o stage/${f%.py}.mpy firmware/$f
done
for f in path/to/micropython-lib/lora/*.py; do $MPY -march=xtensawin -o stage/lib/lora/$(basename ${f%.py}).mpy $f; done
$MPY -march=xtensawin -o stage/example_eink_node.mpy firmware/example_eink_node.py
$MPY -march=xtensawin -o stage/lora_boards.mpy firmware/lora_boards.py
$MPY -march=xtensawin -o stage/wifi_power.mpy firmware/wifi_power.py
cp firmware/config.py stage/   # edit NODE_NAME, board, radio params first
cp firmware/lib/bz2_fast_xtensawin.mpy stage/lib/
mpremote connect /dev/cu.usbserial-0001 fs cp -r stage/urns stage/peripherals stage/lib :/
mpremote connect /dev/cu.usbserial-0001 fs cp stage/example_eink_node.mpy stage/lora_boards.mpy stage/wifi_power.mpy stage/config.py :/
```
