# E-ink status node (`example_eink_node.py`, Heltec Wireless Paper)

Runs the LXMF node and paints a read-only status screen on the Wireless Paper's 2.13" SSD1682 e-ink: a QR code of the node's `lxma://` contact URI (scan it in Sideband or MeshChat), name and short hash, RX/TX counters, last RSSI/SNR, radio settings, uptime and battery. It redraws when the counters change or every 30 s (fast refresh, full refresh every 10th time).

Set the LoRa interface's `"board": "heltec_wireless_paper"` and check the `EINK` pin dict in `config.py`. The driver (`peripherals/eink_ssd1682.py`) reads the panel chip ID and refuses the other 2.13" revision.

The board has **no PSRAM** (about 220 KB of heap), so deploy it as `mpy-cross -march=xtensawin` bytecode and leave the native Ed25519 module off the device (it costs 95 KB of heap; pure-Python crypto is slower but fits). The full staging and upload script is in [docs/eink-deploy.md](../eink-deploy.md). Then `import example_eink_node`.

[← back to the README](../../README.md#what-can-i-build-with-this)
