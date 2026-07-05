"""ADC analog input reader (battery, light, moisture, ...).

Each channel is named and mapped to a GPIO plus an optional voltage divider.
`ATTN_11DB` is set on ESP32 so the full ~0-3.3 V range is usable — the ESP32
ADC default clamps near ~1.1 V and silently saturates anything above it.

process(content):
  - "sensor"  (the trigger the NomadNet page / a broadcast sends) -> every channel
  - a channel name in the text, e.g. "battery" -> just that channel

Reported voltage is scaled by the channel's divider:  vbat = vpin * divider
(use 2.0 for a 1:1 external resistor divider, 1.0 for a direct connection).

NOTE: not every board can sense its battery. The Seeed XIAO ESP32-S3 (incl. the
Wio-SX1262 "Meshtastic" kit) has NO battery->ADC path — Meshtastic itself ships
it with battery monitoring disabled. Only declare a battery channel on a board
that actually has the divider wired. See lora_boards.battery_config().
"""

channels = {}          # name -> (ADC, divider)
_SAMPLES = 8           # averaged per read; the ESP32-S3 ADC is noisy


def init(adc_map, dividers=None, atten="11db"):
    """adc_map = {"battery": 1, "light": 2}; dividers = {"battery": 2.0}.

    Each channel stores (ADC, divider); divider defaults to 1.0 (direct connect).
    """
    from machine import ADC, Pin
    dividers = dividers or {}
    for name, pin_num in adc_map.items():
        adc = ADC(Pin(pin_num))
        try:
            _attn = {"0db": ADC.ATTN_0DB, "2_5db": ADC.ATTN_2_5DB,
                     "6db": ADC.ATTN_6DB, "11db": ADC.ATTN_11DB}
            adc.atten(_attn.get(atten, ADC.ATTN_11DB))
        except AttributeError:
            pass   # ports without configurable attenuation (e.g. RP2040)
        channels[name] = (adc, float(dividers.get(name, 1.0)))


def init_battery(config):
    """Configure the 'battery' channel from the active board's preset
    (lora_boards.battery_config) — the board owns the pin + divider. NO-OP when
    the board declares no battery (e.g. stock XIAO ESP32-S3), so callers can call
    this unconditionally and just list this module as a peripheral; process() and
    battery_voltage() return None when nothing is wired. Returns the params/None."""
    from lora_boards import battery_config
    bat = battery_config(config)
    if bat:
        init({"battery": bat["pin"]}, dividers={"battery": bat.get("divider", 1.0)})
    return bat


def _raw(adc):
    s = 0
    for _ in range(_SAMPLES):
        s += adc.read_u16()
    return s // _SAMPLES


def read_voltage(name):
    """Scaled voltage (float) for one channel, or None if not configured.

    Handy for code that wants the number rather than the formatted string
    (e.g. the transport router's web dashboard battery gauge)."""
    ch = channels.get(name)
    if not ch:
        return None
    adc, divider = ch
    return _raw(adc) * 3.3 / 65535 * divider


def battery_voltage():
    """Battery-channel voltage (float), or None if no battery is configured."""
    return read_voltage("battery")


def _read(name, adc, divider):
    raw = _raw(adc)
    return "{}: {:.2f}V (raw {})".format(name, raw * 3.3 / 65535 * divider, raw)


def process(content):
    if not channels:
        return None
    c = content.lower()
    # Generic trigger -> report all channels (the NomadNet page sends "sensor")
    if "sensor" in c:
        return "\n  ".join(_read(n, a, d) for n, (a, d) in channels.items())
    # Named trigger -> just that channel (command text, or explicit process("battery"))
    for name, (adc, divider) in channels.items():
        if name in c:
            return _read(name, adc, divider)
    return None
