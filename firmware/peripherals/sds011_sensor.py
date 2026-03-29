"""SDS011 particulate matter sensor over UART.

Periodically wakes sensor, reads PM2.5/PM10, then sleeps it again.
Fan only runs briefly during measurement (~5s every interval).
"""

sensor = None
_uart = None
_last_reading = None  # cached (pm25, pm10) tuple
_interval = 300


def init(uart_id=1, tx_pin=43, rx_pin=44, interval=300):
    global sensor, _uart, _interval
    import gc
    from machine import UART
    import sensors.sds011 as sds011
    _uart = UART(uart_id, baudrate=9600, tx=tx_pin, rx=rx_pin)
    sensor = sds011.SDS011(_uart)
    sensor.sleep()
    _interval = interval
    gc.collect()


def start():
    """Start periodic measurement loop. Call from inside async event loop."""
    import uasyncio as asyncio
    asyncio.create_task(_measure_loop())


async def _measure_loop():
    import uasyncio as asyncio
    import gc
    global _last_reading

    # Take first reading soon after boot
    await asyncio.sleep(5)

    while True:
        try:
            gc.collect()
            sensor.wake()
            print("[SDS011] wake, waiting 5s...")
            # Sensor is in active mode — streams data every 1s once awake
            await asyncio.sleep(5)
            gc.collect()
            ok = False
            for _ in range(5):
                ok = sensor.read()
                if ok:
                    _last_reading = (sensor.pm25, sensor.pm10)
                    print("[SDS011] PM2.5:", sensor.pm25, "PM10:", sensor.pm10)
                    break
                await asyncio.sleep(1)
            sensor.sleep()
            gc.collect()
        except Exception as e:
            print("SDS011 error:", e)
        await asyncio.sleep(_interval)


def process(content):
    if sensor and "sensor" in content.lower():
        if _last_reading:
            return "PM2.5: {:.1f} ug/m3, PM10: {:.1f} ug/m3".format(
                _last_reading[0], _last_reading[1])
        return "SDS011: waiting for first reading"
    return None
