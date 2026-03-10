# peripherals — modular hardware drivers for uReticulum examples
# Each module follows the same contract:
#   init(...)          — set up hardware (pins, bus, etc.)
#   process(content)   — handle a message, return response string or None
