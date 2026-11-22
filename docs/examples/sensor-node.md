# Sensor node with deepsleep (`example_sensor.py`)

A minimal LXMF client that boots, reads a sensor (e.g. BME280, SDS011), sends the reading to a fixed hub address, and goes back to deepsleep. Set `SENSOR_HUB = "<hex destination hash>"` in `config.py` (the collector's LXMF address, from its boot log or MeshChat). `import example_sensor`, or save it as `main.py` to restart on every wake. The sleep interval is set inside the example.

[← back to the README](../../README.md#what-can-i-build-with-this)
