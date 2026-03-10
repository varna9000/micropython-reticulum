"""GPIO pin control — on/off and state query."""

pins = {}


def init(pin_map):
    """pin_map = {"lamp": (2, "OUT"), "button": (3, "IN")}"""
    from machine import Pin
    for name, (num, mode) in pin_map.items():
        pins[name] = Pin(num, Pin.OUT if mode == "OUT" else Pin.IN)


def process(content):
    low = content.lower()
    for name, pin in pins.items():
        if name in low:
            if "on" in low:
                pin.value(1)
                return name + ": ON"
            elif "off" in low:
                pin.value(0)
                return name + ": OFF"
            elif "?" in content:
                return name + ": " + ("ON" if pin.value() else "OFF")
    return None
