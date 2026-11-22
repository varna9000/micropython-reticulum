# NomadNet page server (`example_nomadnet_node.py`)

Serves [micron-format](https://github.com/markqvist/NomadNet) pages over Reticulum Links, a tiny web server reachable only via Reticulum. Any ESP32-S3 (WiFi, LoRa or TCP), standard MicroPython, optional I²C/UART/ADC sensors.

Open NomadNet or MeshChat on another machine, wait for the `nomadnetwork.node` announce, and browse to the node. The landing page is `firmware/pages/index.mu`; drop more `.mu` files into `firmware/pages/` to add pages. Template variables: `{node_name}`, `{mem_free}`, `{uptime}`, and `{sensor}` (readings from active [peripherals](../../README.md#peripherals)).

Pages under ~417 bytes ride a single encrypted link packet; larger pages and files (up to 16 KB) use the Resource protocol automatically. Files in `firmware/files/` are served at `/file/<name>`; link to them with `` [label`:/file/name] ``.

[← back to the README](../../README.md#what-can-i-build-with-this)
