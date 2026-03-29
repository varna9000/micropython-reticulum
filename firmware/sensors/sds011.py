"""SDS011 PM2.5/PM10 sensor driver for MicroPython (UART, 9600 baud)."""

# Pre-built 19-byte commands: header(2) + cmd(1) + mode(1) + param(1) + padding(10x00 + ff ff) + checksum(1) + tail(1)
_CMD_QUERY = b'\xaa\xb4\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x02\xab'
_CMD_SLEEP = b'\xaa\xb4\x06\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x05\xab'
_CMD_WAKE  = b'\xaa\xb4\x06\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x06\xab'
_CMD_QMODE = b'\xaa\xb4\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xff\x02\xab'


class SDS011:

    def __init__(self, uart):
        self._uart = uart
        self.pm25 = 0.0
        self.pm10 = 0.0
        uart.write(_CMD_QMODE)

    def wake(self):
        self._uart.write(_CMD_WAKE)

    def sleep(self):
        self._uart.write(_CMD_SLEEP)

    def read(self):
        """Send query, wait for response, parse. Returns True on success."""
        self._uart.write(_CMD_QUERY)
        uart_read = self._uart.read
        for _ in range(512):
            b = uart_read(1)
            if b == b'\xaa':
                if uart_read(1) == b'\xc0':
                    p = uart_read(8)
                    if p and len(p) == 8:
                        pm25_raw = p[0] | (p[1] << 8)
                        pm10_raw = p[2] | (p[3] << 8)
                        chk = (p[0] + p[1] + p[2] + p[3] + p[4] + p[5]) & 0xFF
                        if chk == p[6] and p[7] == 0xAB:
                            self.pm25 = pm25_raw / 10.0
                            self.pm10 = pm10_raw / 10.0
                            return True
        return False
