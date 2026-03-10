import machine
import st7789py as st7789
import vga2_8x16 as font
import utime

history_buf = []
cmd_buf = bytearray()
print("Keyboard ready ..\n")

MAX_WIDTH = 318
MAX_HEIGHT = 238
clean_input = '{: <40}'.format("")

spi = machine.SPI(1, baudrate=8000000, sck=machine.Pin(40), mosi=machine.Pin(41))
DC = machine.Pin(11, machine.Pin.OUT)
CS = machine.Pin(12, machine.Pin.OUT)
BL = machine.Pin(42, machine.Pin.OUT)

BL.value(1)  # force backlight on before init

tft = st7789.ST7789(
    spi,
    240,
    320,
    dc=DC,
    cs=CS,
    backlight=BL,
    rotation=1
)

kbd_pwr = machine.Pin(10, machine.Pin.OUT)
kbd_int = machine.Pin(46, machine.Pin.IN)
i2c = machine.SoftI2C(scl=machine.Pin(8), sda=machine.Pin(18), freq=400000, timeout=50000)


def get_key():
    try:
        return i2c.readfrom(0x55, 1)
    except OSError:
        return b'\x00'


def split_rows(input_string, row_delimiter='\r', chunk_size=40):
    rows = input_string.split(row_delimiter)
    for row in rows:
        for i in range(0, max(len(row), 1), chunk_size):
            chunk = row[i:i + chunk_size]
            history_buf.append(chunk)
    if len(history_buf) > 11:
        del history_buf[0:len(history_buf) - 11]


def chat_history(buf):
    split_rows(buf.decode())
    for i, txt in enumerate(history_buf):
        y = (i + 1) * 16
        if txt[0:3] == 'me>':
            tft.text(font, txt[0:3], 0, y, st7789.GREEN, st7789.BLACK)
            tft.text(font, txt[3:], 24, y, st7789.WHITE, st7789.BLACK)
        elif txt[0:4] == 'oth>':
            tft.text(font, txt[0:4], 0, y, st7789.RED, st7789.BLACK)
            tft.text(font, txt[4:], 32, y, st7789.WHITE, st7789.BLACK)
        else:
            tft.text(font, txt, 0, y, st7789.WHITE, st7789.BLACK)


def cmd_line(buf):
    tft.text(font, buf.decode(), 0, 222, st7789.WHITE, st7789.BLACK)


def draw_navbar(bat, rssi=0):
    tft.fill_rect(0, 0, 320, 16, st7789.BLUE)
    tft.text(font, f"bat:{bat}V", 0, 0, st7789.WHITE, st7789.BLUE)
    tft.text(font, f"rssi:{rssi}", 90, 0, st7789.WHITE, st7789.BLUE)


# Enable keyboard
kbd_pwr.on()
utime.sleep(1)

# Drain garbage bytes from keyboard startup
print("Draining keyboard buffer...")
for _ in range(20):
    get_key()
    utime.sleep_ms(20)

# Draw UI
tft.fill_rect(0, 206, 320, 2, st7789.BLUE)
draw_navbar(0)
print("UI drawn, entering loop")

while True:
    a = get_key()
    if a != b'\x00':
        if a == b'\x08':
            # Backspace: delete last char
            cmd_buf = cmd_buf[:-1]
            tft.text(font, clean_input, 0, 222, st7789.BLACK, st7789.BLACK)
            tft.text(font, cmd_buf.decode(), 0, 222, st7789.WHITE, st7789.BLACK)
        elif a == b'\r':
            # Enter: send message
            print("Sending...")
            chat_history(b'me> ' + cmd_buf)
            tft.text(font, clean_input, 0, 222, st7789.BLACK, st7789.BLACK)
            # modem.send(cmd_buf)
            cmd_buf = bytearray()
        else:
            cmd_buf += a
            cmd_line(cmd_buf)

#     rx = modem.recv(300)
#     if rx:
#         print(f"Received: {rx}")
#         chat_history(b'oth> ' + rx)
#         draw_navbar(bat, rx.rssi)
#         tft.text(font, clean_input, 0, 222, st7789.BLACK, st7789.BLACK)

kbd_pwr.off()