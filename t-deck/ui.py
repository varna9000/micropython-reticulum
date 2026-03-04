# T-Deck GUI Module
# Async state machine: node list + chat screens
# Diff-based drawing: only redraws changed rows. Async yields between rows.

import time
import gc
import uasyncio as asyncio
from machine import Pin, ADC

# Screen states
STATE_NODES = 0
STATE_CHAT  = 1

# Layout constants (320x240 landscape, 8x16 font)
SCREEN_W = 320
SCREEN_H = 240
CHAR_W = 8
CHAR_H = 16
COLS = 40          # 320 / 8
NAV_H = 16        # navbar height (1 row)
SEP_Y = 208       # separator y
INPUT_Y = 212      # input line y (below separator)
BODY_Y = NAV_H    # main area starts after navbar
BODY_ROWS = 12    # (208 - 16) / 16 = 12 rows for content

# Data limits
MAX_PEERS = 16
MAX_HISTORY = 30  # per peer

# Trackball debounce
_TB_DEBOUNCE_MS = 80

# Pad string to exact width (no clearing needed)
def _pad(s, width=COLS):
    if len(s) >= width:
        return s[:width]
    return s + ' ' * (width - len(s))


class UI:

    def __init__(self, tft, font, get_key_func, node_name="T-Deck"):
        self.tft = tft
        self.font = font
        self.get_key = get_key_func
        self.node_name = node_name

        # Import colors from st7789py
        import st7789py as st
        self.BLACK   = st.BLACK
        self.WHITE   = st.WHITE
        self.RED     = st.RED
        self.GREEN   = st.GREEN
        self.BLUE    = st.BLUE
        self.CYAN    = st.CYAN
        self.YELLOW  = st.YELLOW
        self.MAGENTA = st.MAGENTA
        # Selection highlight: dark grey (RGB565)
        self.SEL_BG  = 0x2945

        # State
        self.state = STATE_NODES
        self.dirty = True
        self._input_dirty = False
        self._prev_state = -1  # force full clear on first draw
        self._state_change_ms = 0  # debounce rapid state flips

        # Row cache: 15 slots (navbar + 12 body + sep + input)
        # Compared before drawing — skip SPI if row unchanged.
        self._cache = [''] * 15

        # Peers: dest_hash_bytes -> {"name": str, "rssi": int}
        self.peers = {}
        self._peer_keys = []  # ordered list of dest_hash_bytes
        self.selected_idx = 0
        self.node_scroll = 0

        # Chat: dest_hash_bytes -> [(is_mine, text, timestamp), ...]
        self.chat_history = {}
        self.chat_scroll = 0
        self.selected_peer = None  # dest_hash_bytes of current chat peer

        # Input
        self.cmd_buf = bytearray()

        # Navbar state
        self.bat_v = 0.0
        self.rssi = None
        self.announce_flash = 0  # timestamp of last announce flash

        # Trackball pins
        self._tb_up    = Pin(3, Pin.IN, Pin.PULL_UP)
        self._tb_down  = Pin(15, Pin.IN, Pin.PULL_UP)
        self._tb_left  = Pin(1, Pin.IN, Pin.PULL_UP)
        self._tb_right = Pin(2, Pin.IN, Pin.PULL_UP)
        self._tb_click = Pin(0, Pin.IN, Pin.PULL_UP)
        self._tb_last  = 0  # scroll debounce timestamp (ms)
        self._tb_click_last = 0  # click debounce (separate)
        self._tb_prev_up = True    # previous pin states (for edge detection)
        self._tb_prev_down = True
        self._tb_prev_click = True

        # Battery ADC
        self._bat_adc = ADC(Pin(4))
        self._bat_adc.atten(ADC.ATTN_11DB)

        # Callbacks (set by tdeck_node.py)
        self.on_send = None       # on_send(dest_hash_bytes, text)
        self.on_announce = None   # on_announce()

    # --- Drawing helpers ---

    def _draw_row_cached(self, idx, text, y, fg, bg=None):
        """Draw row only if content changed. Returns True if drawn."""
        if self._cache[idx] == text:
            return False
        self._cache[idx] = text
        self.tft.text(self.font, _pad(text), 0, y, fg, bg or self.BLACK)
        return True

    def _row(self, text, y, fg, bg=None):
        """Draw a full-width padded row — overwrites old content, no flicker."""
        self.tft.text(self.font, _pad(text), 0, y, fg, bg or self.BLACK)

    def _text(self, text, x, y, fg, bg=None):
        """Draw text at pixel position."""
        self.tft.text(self.font, text, x, y, fg, bg or self.BLACK)

    # --- Navbar ---

    def draw_navbar(self):
        # Build full navbar string, padded to COLS
        bat_str = "bat:{:.1f}V".format(self.bat_v)
        rssi_str = "rssi:" + str(self.rssi) if self.rssi is not None else ""
        name = self.node_name[:12]
        ann = "[A]" if (self.announce_flash and time.time() - self.announce_flash < 2) else "   "

        # Fixed-position layout: bat(12) rssi(12) name(13) ann(3) = 40
        nav = "{:<12}{:<12}{:>13}{}".format(bat_str, rssi_str, name, ann)
        self._draw_row_cached(0, nav, 0, self.WHITE, self.BLUE)

    # --- Node list screen ---

    def draw_node_list(self):
        if not self._peer_keys:
            self._draw_row_cached(1, "No peers yet.", BODY_Y, self.WHITE)
            self._draw_row_cached(2, "Waiting for announces...", BODY_Y + CHAR_H, self.WHITE)
            for i in range(2, BODY_ROWS):
                self._draw_row_cached(i + 1, "", BODY_Y + i * CHAR_H, self.WHITE)
        else:
            visible = self._peer_keys[self.node_scroll:self.node_scroll + BODY_ROWS]
            for i in range(BODY_ROWS):
                y = BODY_Y + i * CHAR_H
                if i < len(visible):
                    key = visible[i]
                    peer = self.peers[key]
                    name = peer.get("name") or "?"
                    hash_str = key.hex()[:8]
                    line = "{:<24} [{}]".format(name[:24], hash_str)

                    abs_idx = self.node_scroll + i
                    if abs_idx == self.selected_idx:
                        cache_key = '\x01' + line  # \x01 = selected marker
                        if self._cache[i + 1] != cache_key:
                            self._cache[i + 1] = cache_key
                            self.tft.text(self.font, _pad(line), 0, y, self.YELLOW, self.SEL_BG)
                    else:
                        self._draw_row_cached(i + 1, line, y, self.WHITE, self.BLACK)
                else:
                    self._draw_row_cached(i + 1, "", y, self.WHITE)

        # Separator
        self.tft.fill_rect(0, SEP_Y, SCREEN_W, 2, self.BLUE)

        # Status bar
        self._draw_row_cached(14, "Enter=chat  a=announce", INPUT_Y, self.CYAN)

    # --- Chat screen ---

    def _build_chat_lines(self):
        """Build word-wrapped display lines for current chat."""
        if self.selected_peer is None:
            return []

        msgs = self.chat_history.get(self.selected_peer, [])
        lines = []
        for is_mine, text, _ts in msgs:
            if is_mine:
                prefix = "me> "
            else:
                peer = self.peers.get(self.selected_peer)
                pname = (peer.get("name") or "?")[:8]
                prefix = pname + "> "
            wrapped = self._wrap_text(prefix + text, COLS)
            for j, wl in enumerate(wrapped):
                lines.append((is_mine, wl, j == 0))
        return lines

    def draw_chat(self):
        lines = self._build_chat_lines()

        # Clamp scroll — can't scroll past all content
        total = len(lines)
        max_scroll = max(0, total - 1)
        if self.chat_scroll > max_scroll:
            self.chat_scroll = max_scroll

        # Apply scroll — show last N lines, scrollable up
        view_end = max(0, total - self.chat_scroll)
        view_start = max(0, view_end - BODY_ROWS)
        visible = lines[view_start:view_end]

        for i in range(BODY_ROWS):
            y = BODY_Y + i * CHAR_H
            if i < len(visible):
                is_mine, text, is_first = visible[i]
                cached = self._cache[i + 1] == text
                if cached:
                    continue  # skip unchanged row
                self._cache[i + 1] = text

                padded = _pad(text)
                if is_first:
                    # Optimized: draw full line WHITE, overdraw short prefix in color
                    self.tft.text(self.font, padded, 0, y, self.WHITE, self.BLACK)
                    if is_mine:
                        self.tft.text(self.font, text[:4], 0, y, self.GREEN, self.BLACK)
                    else:
                        gt = text.find(">")
                        if gt >= 0:
                            self.tft.text(self.font, text[:gt + 1], 0, y, self.RED, self.BLACK)
                else:
                    self.tft.text(self.font, padded, 0, y, self.WHITE, self.BLACK)
            else:
                self._draw_row_cached(i + 1, "", y, self.WHITE)

        # Separator
        self.tft.fill_rect(0, SEP_Y, SCREEN_W, 2, self.BLUE)

        # Input line
        self.draw_input()

    def draw_input(self):
        prompt = "> "
        inp = self.cmd_buf.decode()
        line = prompt + inp[:COLS - 3] + "_"
        self.tft.text(self.font, _pad(line), 0, INPUT_Y, self.GREEN, self.BLACK)
        # Overwrite message text portion in white (after prompt)
        if inp:
            self._text(inp[:COLS - 3], 2 * CHAR_W, INPUT_Y, self.WHITE)

    # --- Text wrapping ---

    @staticmethod
    def _wrap_text(text, width):
        """Simple word wrap. Returns list of strings."""
        if len(text) <= width:
            return [text]
        lines = []
        while text:
            if len(text) <= width:
                lines.append(text)
                break
            idx = text.rfind(" ", 0, width)
            if idx <= 0:
                idx = width
            lines.append(text[:idx])
            text = text[idx:].lstrip(" ")
        return lines

    # --- Input handling ---

    def handle_key(self, key):
        """Handle a keyboard key press. Returns True if UI needs redraw."""
        ch = key[0]

        if ch == 0:
            return False

        if self.state == STATE_NODES:
            return self._handle_key_nodes(ch, key)
        else:
            return self._handle_key_chat(ch, key)

    def _handle_key_nodes(self, ch, key):
        if ch == 0x0D:  # Enter — select peer, go to chat
            if self._peer_keys and 0 <= self.selected_idx < len(self._peer_keys):
                self.selected_peer = self._peer_keys[self.selected_idx]
                self.chat_scroll = 0
                self.cmd_buf = bytearray()
                self.state = STATE_CHAT
                self._state_change_ms = time.ticks_ms()
                self.dirty = True
                return True
        elif key == b'a' or key == b'A':
            if self.on_announce:
                self.on_announce()
            self.announce_flash = time.time()
            self.dirty = True
            return True
        return False

    def _handle_key_chat(self, ch, key):
        if ch == 0x08:  # Backspace
            if len(self.cmd_buf) > 0:
                self.cmd_buf = self.cmd_buf[:-1]
                self._input_dirty = True
            elif time.ticks_diff(time.ticks_ms(), self._state_change_ms) > 500:
                # Empty input + backspace = return to node list
                # (500ms guard prevents phantom keyboard bytes from flipping back)
                self.state = STATE_NODES
                self.chat_scroll = 0
                self._state_change_ms = time.ticks_ms()
                self.dirty = True
            return True
        elif ch == 0x0D:  # Enter — send message
            if len(self.cmd_buf) > 0 and self.selected_peer:
                text = self.cmd_buf.decode()
                self.cmd_buf = bytearray()
                self.add_chat_message(self.selected_peer, True, text)
                if self.on_send:
                    self.on_send(self.selected_peer, text)
                self.dirty = True
            return True
        elif ch == 0x1B:  # Escape — back to node list
            if time.ticks_diff(time.ticks_ms(), self._state_change_ms) > 500:
                self.state = STATE_NODES
                self.chat_scroll = 0
                self._state_change_ms = time.ticks_ms()
                self.dirty = True
            return True
        elif 0x20 <= ch < 0x7F:  # Printable
            self.cmd_buf += key
            self._input_dirty = True
            return True
        return False

    def handle_trackball(self):
        """Poll trackball GPIOs with edge detection + debounce."""
        now = time.ticks_ms()
        action = False

        # Read current pin states
        up_now = self._tb_up.value()
        down_now = self._tb_down.value()
        click_now = self._tb_click.value()

        # Click — edge detect: trigger on HIGH->LOW transition
        if not click_now and self._tb_prev_click and time.ticks_diff(now, self._tb_click_last) >= 200:
            if self.state == STATE_NODES:
                self.handle_key(b'\x0D')
            self._tb_click_last = now
            action = True

        # Scroll — edge detect: trigger on HIGH->LOW transition + debounce
        if not action and time.ticks_diff(now, self._tb_last) >= _TB_DEBOUNCE_MS:
            if not up_now and self._tb_prev_up:
                self._scroll_up()
                self._tb_last = now
                action = True
            elif not down_now and self._tb_prev_down:
                self._scroll_down()
                self._tb_last = now
                action = True

        # Store pin states for next edge detection
        self._tb_prev_up = up_now
        self._tb_prev_down = down_now
        self._tb_prev_click = click_now

        if action:
            self.dirty = True
        return action

    def _scroll_up(self):
        if self.state == STATE_NODES:
            if self.selected_idx > 0:
                self.selected_idx -= 1
                if self.selected_idx < self.node_scroll:
                    self.node_scroll = self.selected_idx
        else:
            # Capped by draw_chat to actual content length
            if self.chat_scroll < MAX_HISTORY * 3:
                self.chat_scroll += 1

    def _scroll_down(self):
        if self.state == STATE_NODES:
            if self.selected_idx < len(self._peer_keys) - 1:
                self.selected_idx += 1
                if self.selected_idx >= self.node_scroll + BODY_ROWS:
                    self.node_scroll = self.selected_idx - BODY_ROWS + 1
        else:
            if self.chat_scroll > 0:
                self.chat_scroll -= 1

    # --- Data management ---

    def add_peer(self, dest_hash, name, rssi=None):
        """Add or update a peer from an announce."""
        if dest_hash not in self.peers:
            if len(self.peers) >= MAX_PEERS:
                oldest = self._peer_keys[0]
                del self.peers[oldest]
                self._peer_keys.pop(0)

        self.peers[dest_hash] = {"name": name or "?", "rssi": rssi}
        if dest_hash not in self._peer_keys:
            self._peer_keys.append(dest_hash)
        self.dirty = True

    def add_chat_message(self, dest_hash, is_mine, text):
        """Add a message to chat history for a peer."""
        if dest_hash not in self.chat_history:
            self.chat_history[dest_hash] = []
        hist = self.chat_history[dest_hash]
        hist.append((is_mine, text, time.time()))
        if len(hist) > MAX_HISTORY:
            hist.pop(0)

        if self.state == STATE_CHAT:
            if self.selected_peer == dest_hash:
                self.chat_scroll = 0
            # Invalidate body row cache — lines shift when new message arrives
            for i in range(1, BODY_ROWS + 1):
                self._cache[i] = ''
            self.dirty = True

    def update_battery(self):
        """Read battery voltage from ADC. T-Deck has voltage divider (x2)."""
        raw = self._bat_adc.read()
        self.bat_v = raw * 3.3 * 2 / 4095

    # --- Main draw ---

    def draw(self):
        """Cached screen redraw — skips unchanged rows."""
        # Clear body + invalidate cache on screen state change
        if self.state != self._prev_state:
            self.tft.fill_rect(0, BODY_Y, SCREEN_W, SCREEN_H - BODY_Y, self.BLACK)
            self._cache = [''] * 15  # invalidate all rows
            self._prev_state = self.state

        self.draw_navbar()
        if self.state == STATE_NODES:
            self.draw_node_list()
        else:
            self.draw_chat()
        self.dirty = False
        self._input_dirty = False

    # --- Async loops ---

    async def kbd_loop(self):
        """Fast keyboard + trackball polling — independent of drawing."""
        while True:
            for _ in range(5):  # drain up to 5 keys per cycle
                key = self.get_key()
                if key == b'\x00':
                    break
                self.handle_key(key)
            self.handle_trackball()
            await asyncio.sleep_ms(10)

    async def gui_loop(self, spi_acquire_display, spi_release_display):
        """Drawing + input loop. kbd_loop handles fast polling separately."""
        self._last_draw = 0

        # Initial draw
        spi_acquire_display()
        self.draw()
        spi_release_display()

        while True:
            # Redraw: immediate for input line, throttled for full redraws
            now = time.ticks_ms()
            if self._input_dirty and not self.dirty:
                spi_acquire_display()
                self.draw_input()
                spi_release_display()
                self._input_dirty = False
            elif self.dirty and time.ticks_diff(now, self._last_draw) > 80:
                spi_acquire_display()
                self.draw()
                spi_release_display()
                self._last_draw = now

            await asyncio.sleep_ms(20)

    async def battery_loop(self, spi_acquire_display, spi_release_display):
        """Update battery reading every 10s."""
        while True:
            self.update_battery()
            spi_acquire_display()
            self.draw_navbar()
            spi_release_display()
            await asyncio.sleep(10)
