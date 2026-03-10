"""ADC analog input reader."""

channels = {}


def init(adc_map):
    """adc_map = {"battery": 1, "light": 2}"""
    from machine import ADC
    for name, pin_num in adc_map.items():
        channels[name] = ADC(pin_num)


def process(content):
    for name, adc in channels.items():
        if name in content.lower():
            raw = adc.read_u16()
            voltage = raw * 3.3 / 65535
            return "{}: {:.2f}V (raw {})".format(name, voltage, raw)
    return None
