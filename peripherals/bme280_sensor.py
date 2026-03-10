"""BME280 temperature/pressure/humidity sensor over I2C."""

bme = None


def init(i2c):
    global bme
    import sensors.bme280 as bme280
    bme = bme280.BME280(i2c=i2c)


def process(content):
    if bme and "sensor" in content.lower():
        t, p, h = bme.values
        return "Temperature: {}, Pressure: {}, Humidity: {}".format(t, p, h)
    return None
