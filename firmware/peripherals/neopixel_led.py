"""NeoPixel RGB LED control."""

led = None
colors = {}


def init(pin, num_leds=1, order="RGB"):
    global led, colors
    from machine import Pin
    import neopixel
    led = neopixel.NeoPixel(Pin(pin), num_leds)

    r, g, b = order.index("R"), order.index("G"), order.index("B")
    colors = {
        "red": tuple(255 if i == r else 0 for i in range(3)),
        "green": tuple(255 if i == g else 0 for i in range(3)),
        "blue": tuple(255 if i == b else 0 for i in range(3)),
        "off": (0, 0, 0),
    }


def process(content):
    if led and content.lower() in colors:
        led[0] = colors[content.lower()]
        led.write()
        return "LED: " + content.lower()
    return None
