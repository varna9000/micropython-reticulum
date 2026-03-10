"""NeoPixel RGB LED control."""

led = None
colors = {
    "green": (255, 0, 0),
    "red": (0, 255, 0),
    "blue": (0, 0, 255),
    "off": (0, 0, 0),
}


def init(pin, num_leds=1):
    global led
    from machine import Pin
    import neopixel
    led = neopixel.NeoPixel(Pin(pin), num_leds)


def process(content):
    if led and content.lower() in colors:
        led[0] = colors[content.lower()]
        led.write()
        return "LED: " + content.lower()
    return None
