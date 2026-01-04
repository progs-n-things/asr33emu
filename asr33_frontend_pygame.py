#!/usr/bin/env python3

"""
ASR-33 Frontend using Pygame.

- Displays scrollback lines from Terminal/Scrollback.
- Page Up / Page Down scroll through history by PAGE_SCROLL_STEP.
- Mouse wheel scrolls one line at a time.
- Home / End jump to top or bottom.
- Uses custom Teleprinter font for authentic ASR-33 look.
- Status overlay when viewing history.
- Integrates ASR33_Sounds for authentic teletype audio.
"""

import contextlib
import sys
import io
import subprocess
import tkinter as tk
import ctypes
try:
    import ctypes.wintypes  # pyright: ignore[reportMissingModuleSource]
except ImportError:
    pass  # Not on Windows

# pylint: disable=no-member
# Suppress the pygame welcome message
with contextlib.redirect_stdout(io.StringIO()):
    import pygame

# pylint: disable=wildcard-import, no-name-in-module, unused-import
from pygame.locals import KEYDOWN, K_PAGEUP, K_PAGEDOWN, K_HOME, K_END, QUIT, MOUSEBUTTONDOWN

from asr33_config import ASR33Config
from asr33_backend_ssh import SSHV2Backend
from asr33_backend_serial import SerialBackend
from asr33_shim_throttle import DataThrottle
from asr33_terminal import Terminal
from asr33_sounds_sm import ASR33AudioModule as ASR33_Sounds
from asr33_papertape import PapertapeReader, PapertapePunch

# Default terminal constants

# True for true ASR-33 emulation.
# False if you want to allow lowercase input from keyboard.
KEYBOARD_UPPERCASE_ONLY = False

# Controls bit-7 generation. Options are ("mark", "space", "even")
# "mark" seems to be the best choice for compatibility with DEC programs.
KEYBOARD_PARITY_MODE = "space"

# True to send carriage return to host on startup to wake it up
SEND_CR_AT_STARTUP = False

# Default terminal layout constants
MARGIN = 20
COLUMNS = 72
ROWS = 20
SCROLLBACK_LINES = 200
PAGE_SCROLL_STEP = 12
MOUSE_SCROLL_STEP = 3

BG_COLOR = (0xff, 0xee, 0xdd)
TEXT_COLOR = (0x33, 0x33, 0x33)

# Default font settings
DEFAULT_FONT_PATH = "Teletype33.ttf"
DEFAULT_FONT_SIZE = 20


class ASR33PygameFrontend:
    """Pygame-based frontend for ASR-33 emulator."""
    def __init__(self, terminal, backend, config, sound=None):
        self._term = terminal
        self._backend = backend
        self._sounds = sound
        self.cfg = config

        self._lid_state = self.cfg.sound.config.lid
        self._sound_mute_state = self.cfg.sound.config.get("mute_state", default="unmuted")
        self._data_rate = self.cfg.data_throttle.config.get("mode", default="throttled")
        self._loopback_state = self.cfg.terminal.config.get("mode", default="line")
        self._printer_state = "on"
        if self.cfg.terminal.config.get("no_print", default=False):
            self._printer_state = "off"
        self.keyboard_uppercase_only = self.cfg.terminal.config.get(
            "keyboard_uppercase_only",
             default=KEYBOARD_UPPERCASE_ONLY)
        self.keyboard_parity_mode = self.cfg.terminal.config.get(
            "keyboard_parity_mode",
            default=KEYBOARD_PARITY_MODE)
        self.send_cr_at_startup = self.cfg.terminal.config.get(
            "send_cr_at_startup",
            default=SEND_CR_AT_STARTUP)

        self.font_path = self.cfg.terminal.config.get(
            "font_path",
            default=None)
        if self.font_path is None:
            self.font_path = DEFAULT_FONT_PATH

        font_scale = 1.4 # scale factor to match Tkinter font size
        self.font_size = int(font_scale*self.cfg.terminal.config.get(
            "font_size",
            default=DEFAULT_FONT_SIZE))

        pygame.init()

        # papertape devices
        self.tape_running_state = False

        self.root = tk.Tk()
        self.root.title("this should be hidden")
        self.root.withdraw()  # hide blank root window
        self.root.bind_all("<KeyPress>", self.forward_key)  # forward Tk keypress to pygame

        self.papertape_punch = PapertapePunch(
            master=self.root,
            config=self.cfg.tape_punch.config
        )
        self.papertape_reader = PapertapeReader(
            master=self.root,
            backend=self._backend,
            config=self.cfg.tape_reader.config
        )

        self.font = pygame.font.Font(self.font_path, self.font_size)
        self.text_color = TEXT_COLOR

        # Character dimensions
        self.char_w, self.char_h = self.font.size("X")
        # Adjust character height to match ASR-33 aspect ratio of 10 CPI / 6 LPI
        self.char_h = self.char_w * 10 // 6
        # Window size based on terminal width/height plus margins
        self.win_w = self.char_w * self._term.width + 2 * MARGIN
        self.win_h = self.char_h * self._term.height + 2 * MARGIN
        self.screen = pygame.display.set_mode((self.win_w, self.win_h))

        # Fill with background color right away
        self.screen.fill(BG_COLOR)
        pygame.display.flip()   # push it to the window immediately
        self.window_caption = f"ASR-33 Emulator using {self._backend.get_info_string()}"
        pygame.display.set_caption(self.window_caption)

        # scroll state
        self.display_update_needed = True
        self.screen_top_lln = None
        self.overstrike_enabled = True

        if not self._backend is None and self.send_cr_at_startup:
            self._backend.send_data(b'\r') # send CR to wake up host

    def get_window_position(self):
        """Get the (x,y) position of the Pygame window on the desktop."""
        title = pygame.display.get_caption()[0]
        if sys.platform.startswith("win"):
            # --- Windows (Win32 API) ---
            hwnd = ctypes.windll.user32.FindWindowW(None, title)
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            return rect.left, rect.top

        if sys.platform.startswith("linux"):
            # --- Linux (X11) ---
            try:
                output = subprocess.check_output(
                    ["xwininfo", "-name", title],
                    universal_newlines=True
                )
                x, y = 100, 100 # default fallback
                for line in output.splitlines():
                    if "Absolute upper-left X:" in line:
                        x = int(line.split(":")[1].strip())
                    if "Absolute upper-left Y:" in line:
                        y = int(line.split(":")[1].strip()) + 12
                return x, y
            except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
                # xwininfo failed, not present, or returned unexpected output; fall back to defaults
                pass

        return 100,100  # Default fallback position


    # Mapping Tkinter keysym → pygame key constants
    keymap = {
        "Left": pygame.K_LEFT,
        "Right": pygame.K_RIGHT,
        "Up": pygame.K_UP,
        "Down": pygame.K_DOWN,
        "Return": pygame.K_RETURN,
        "space": pygame.K_SPACE,
        "Escape": pygame.K_ESCAPE,
        "Tab": pygame.K_TAB,
        "Shift_L": pygame.K_LSHIFT,
        "Shift_R": pygame.K_RSHIFT,
        "Control_L": pygame.K_LCTRL,
        "Control_R": pygame.K_RCTRL,
        "Alt_L": pygame.K_LALT,
        "Alt_R": pygame.K_RALT,
        # Function keys
        "F1": pygame.K_F1, "F2": pygame.K_F2, "F3": pygame.K_F3, "F4": pygame.K_F4,
        "F5": pygame.K_F5, "F6": pygame.K_F6, "F7": pygame.K_F7, "F8": pygame.K_F8,
        "F9": pygame.K_F9, "F10": pygame.K_F10, "F11": pygame.K_F11, "F12": pygame.K_F12,
        # Navigation keys
        "Home": pygame.K_HOME,
        "End": pygame.K_END,
        "Prior": pygame.K_PAGEUP,   # Tkinter uses "Prior" for Page Up
        "Next": pygame.K_PAGEDOWN,  # Tkinter uses "Next" for Page Down
        "Delete": pygame.K_DELETE
    }

    def tk_to_pygame_key(self, event):
        """Convert Tkinter event to pygame key constant."""
        if event.keysym in self.keymap:
            return self.keymap[event.keysym]
        elif event.char:  # fallback for normal characters
            return ord(event.char)
        return 0

    def forward_key(self, event):
        """Forward Tk a key press events to pygame."""
        key = self.tk_to_pygame_key(event)
        if key:
            pg_event = pygame.event.Event(
                pygame.KEYDOWN,
                {"key": key, "unicode": event.char}
            )
            pygame.event.post(pg_event)

    # --- Papertape reader / punch handlers ---
    def _reader_show_f1(self):
        parent_x, parent_y = self.get_window_position()
        self.papertape_reader.show(
            parent_x=parent_x,
            parent_y=parent_y + self.win_h//2,
            parent_h=self.win_h
        )

    def _reader_hide_f2(self):
        self.papertape_reader.hide()

    def _punch_show_f3(self):
        parent_x, parent_y = self.get_window_position()
        self.papertape_punch.show(
            parent_x=parent_x,
            parent_y=parent_y,
            parent_h=self.win_h
        )

    def _punch_hide_f4(self):
        self.papertape_punch.hide()

    def _throttle_toggle_f5(self):
        self._data_rate = "throttled" if self._data_rate == "unthrottled" else "unthrottled"
        self._manage_throttle()

    def _sound_toggle_mute_f6(self):
        self._sound_mute_state = "muted" if self._sound_mute_state == "unmuted" else "unmuted"
        self._sound_manage_mute()

    def _sound_lid_toggle_f7(self):
        self._lid_state = "down" if self._lid_state == "up" else "up"
        self._sound_manage_lid()

    def _loopback_toggle_f8(self):
        self._loopback_state = "local" if self._loopback_state == "line" else "line"
        self._manage_loopback()

    def _printer_toggle_f9(self):
        self._printer_state = "off" if self._printer_state == "on" else "on"
        self._manage_printer()

    def _sound_manage_lid(self):
        if hasattr(self._sounds, "lid"):
            lid_up = self._lid_state == "up"
            if self._sounds is not None:
                self._sounds.lid(set_lid_to_up=lid_up)

    def _sound_manage_mute(self):
        if self._sounds is not None and hasattr(self._sounds, "mute"):
            self._sounds.mute(self._sound_mute_state == "muted")

    def _manage_throttle(self):
        if (not hasattr(self._backend, "enable_throttling") or
            not hasattr(self._backend, "disable_throttling")):
            return
        if self._data_rate == "throttled":
            self._backend.enable_throttling()
        else:
            self._backend.disable_throttling()

    def _manage_loopback(self):
        if self._loopback_state == "local":
            if hasattr(self._backend, "enable_loopback"):
                self._backend.enable_loopback()
        else:
            if hasattr(self._backend, "disable_loopback"):
                self._backend.disable_loopback()

    def _manage_printer(self):
        if self._printer_state == "off":
            self._term.disable_printing()
        else:
            self._term.enable_printing()

    # --- Input / scrolling ---
    def _scroll_helper(self, steps: int) -> None:
        """Helper to calculate new top logical line number
           based on desired number scroll steps.
        """
        st_lln = self.screen_top_lln

        # screen_top_lln is None when we are at bottom
        if st_lln is None:
            # We are at bottom, so set screen_top_lln to top of last full screen
            st_lln = self._term.line_history.bottom_lln() - self._term.height
            # If history is less than a full screen, clamp to top
            st_lln = max(
                self._term.line_history.top_lln(),
                st_lln
            )

        st_lln = max(self._term.line_history.top_lln(), st_lln + steps)
        if st_lln > self._term.line_history.bottom_lln() - self._term.height:
            st_lln = None  # At bottom
        self.screen_top_lln = st_lln

    def _page_up(self):
        """ scroll up method """
        self._scroll_helper(-PAGE_SCROLL_STEP)
        self.display_update_needed = True
        return "break"

    def _page_down(self):
        """ scroll down method """
        self._scroll_helper(PAGE_SCROLL_STEP)
        self.display_update_needed = True
        return "break"

    def _page_home(self):
        self.screen_top_lln = self._term.line_history.top_lln()
        self.display_update_needed = True
        return "break"

    def _page_end(self):
        self.screen_top_lln = None
        self.display_update_needed = True
        return "break"

    def _handle_key(self, event):
        """Handle a keyboard event"""
        if event.unicode and len(event.unicode) == 1 and ord(event.unicode) < 0xF000:
            ch = event.unicode
            if ch:
                if self.keyboard_uppercase_only:
                    ch = ch.upper()
                byte = ch.encode('ascii')
                if self.keyboard_parity_mode == "even":
                    # ASR-33 keyboard is uppercase only with bit 7 as even parity
                    byte = self._term.encode_even_parity(byte)
                elif self.keyboard_parity_mode == "mark":
                    # ASR-33 keyboard is uppercase only with bit 7 set to 1
                    byte = bytes([byte[0] | 0x80])  # set bit 7
                elif self.keyboard_parity_mode == "space":
                    # ASR-33 keyboard is uppercase only with bit 7 set to 0
                    byte = bytes([byte[0] & 0x7F])  # clear bit 7
                else:  # Unknown mode, send as-is
                    pass
                self._backend.send_data(byte)
                self.screen_top_lln = None # return to bottom of screen on keypress
                if self._sounds is not None and hasattr(self._sounds, "keypress"):
                    self._sounds.keypress()  # play keypress sound

        key_actions = {
            pygame.K_F1: self._reader_show_f1,
            pygame.K_F2: self._reader_hide_f2,
            pygame.K_F3: self._punch_show_f3,
            pygame.K_F4: self._punch_hide_f4,
            pygame.K_F5 : self._throttle_toggle_f5,
            pygame.K_F6: self._sound_toggle_mute_f6,
            pygame.K_F7: self._sound_lid_toggle_f7,
            pygame.K_F8: self._loopback_toggle_f8,
            pygame.K_F9: self._printer_toggle_f9,
            pygame.K_PAGEUP: self._page_up,
            pygame.K_PAGEDOWN: self._page_down,
            pygame.K_HOME: self._page_home,
            pygame.K_END: self._page_end
        }

        action = key_actions.get(event.key)
        if action:
            action()

    def _mouse_scroll(self, event):
        """Handle mouse wheel scrolling."""
        if event.button in (4, 5):  # mouse wheel up/down
            if event.button == 4:  # wheel up
                self._scroll_helper(-MOUSE_SCROLL_STEP)
            elif event.button == 5:  # wheel down
                self._scroll_helper(MOUSE_SCROLL_STEP)

            self.display_update_needed = True

    def _draw_cursor(self):
        """Draw the cursor rectangle"""
        # Get cursor position in logical (terminal) coords
        cursor_x, cursor_y = self._term.get_cursor_position()

        # Determine physical (visible) start line in logical coords
        if self.screen_top_lln is None:
            start = max(0, self._term.line_history.bottom_lln() - self._term.height+1)
        else:
            start = min(
                self.screen_top_lln,
                max(0,  self._term.line_history.top_lln() - self._term.height)
            )

        x0 = cursor_x * self.char_w + MARGIN
        y0 = (cursor_y - start) * self.char_h + MARGIN
        cursor_rect = pygame.Rect(x0,y0,self.char_w,self.char_h)
        pygame.draw.rect(self.screen, self.text_color, cursor_rect, 1)

    def _get_visible_lines(self):
        """Get the list of line objects currently visible on screen."""
        total_lines = len(self._term.line_history)
        if self.screen_top_lln is None:
            start_index = max(0, total_lines - self._term.height)
        else:
            start_index = min(
                self.screen_top_lln - self._term.line_history.top_lln(),
                max(0, total_lines - self._term.height)
            )
            start_index = max(0, start_index)

        end_index = min(start_index + self._term.height, total_lines)
        return [
            self._term.line_history.get_line(i)
            for i in range(start_index, end_index)
        ]

    def _render(self):
        """Render the terminal contents to the Pygame screen."""
        visible = self._get_visible_lines()
        self.screen.fill(BG_COLOR)
        for row, line in enumerate(visible):
            y = MARGIN + row * self.char_h
            for col in range(self._term.width):
                stack = line.get_strike_stack(col)
                if not stack:
                    continue
                if not self.overstrike_enabled:
                    # only render top of stack
                    glyph = self.font.render(stack[-1], True, self.text_color).convert_alpha()
                    self.screen.blit(glyph, (MARGIN + col * self.char_w, y))
                else:
                    for s in stack:
                        glyph = self.font.render(s, True, self.text_color).convert_alpha()
                        self.screen.blit(glyph, (MARGIN + col * self.char_w, y))

        # Draw cursor on screen
        self._draw_cursor()

        # --- Status overlay ---
        if self.screen_top_lln is not None and len(self._term.line_history) > self._term.height:
            overlay_font = pygame.font.Font(self.font_path, self.font_size-4)
            if self.screen_top_lln == self._term.line_history.top_lln():
                text_surface = overlay_font.render("VIEWING HISTORY: TOP", True, (255, 255, 255))
            else:
                text_surface = overlay_font.render("VIEWING HISTORY", True, (255, 255, 255))
            bg_rect = text_surface.get_rect()
            bg_rect.topright = (self.win_w - MARGIN, 0)
            overlay_bg = pygame.Surface((bg_rect.width + 10, bg_rect.height + 4))
            overlay_bg.set_alpha(150)
            overlay_bg.fill((0, 0, 0))
            self.screen.blit(overlay_bg, (bg_rect.left - 5, bg_rect.top - 2))
            self.screen.blit(text_surface, bg_rect)

    def _main_loop(self):
        """Main loop for Pygame frontend."""
        clock = pygame.time.Clock()
        running = True
        while running:
            # Simulate Tkinter mainloop processing
            if self.root is not None:
                try:
                    self.root.update_idletasks()
                    self.root.update()
                except tk.TclError:
                    # Root may have been destroyed concurrently; ignore and continue.
                    pass

            for event in pygame.event.get():
                if event.type == QUIT:
                    running = False
                elif event.type == KEYDOWN:
                    self._handle_key(event)
                elif event.type == MOUSEBUTTONDOWN:
                    self._mouse_scroll(event)

            # play sounds for new characters
            while self._term.sound_queue_len() > 0:
                ch, col = self._term.pop_char_from_sound_queue()
                if self._sounds is not None and hasattr(self._sounds, "print_char"):
                    self._sounds.print_char(ch)
                if col == 62:
                    if self._sounds is not None and hasattr(self._sounds, "column_bell"):
                        self._sounds.column_bell()

            if self.display_update_needed:
                self.display_update_needed = False
                self._render()
                pygame.display.flip()

            for _ in range(10 if self._data_rate == "unthrottled" else 1):
                self.papertape_reader.process()

            if hasattr(self._sounds, "tape_reader_running"):
                tape_running_status = self.papertape_reader.active_status()
                # track state changes to avoid redundant calls
                if self.tape_running_state != tape_running_status:
                    self.tape_running_state = tape_running_status
                    if self._sounds is not None:
                        self._sounds.tape_reader_running(tape_running_status)

            self.papertape_punch.process()

            clock.tick(50)

    def receive_data(self, data: bytes):
        """Data delivered from terminal"""
        # Punch data to papertape punch if active
        self.papertape_punch.punch_bytes(data)
        self.display_update_needed = True

    def run(self):
        """Run the Pygame frontend."""

        # Initial sound, backend and printer state management
        self._sound_manage_lid()
        self._sound_manage_mute()
        self._manage_throttle()
        self._manage_loopback()
        self._manage_printer()

        if not self._backend is None and hasattr(self._backend, "start"):
            self._backend.start()

        # sounds
        if (self._sounds and hasattr(self._sounds, "start")):
            self._sounds.start()

        self._main_loop()

        # Cleanup on exit
        self.papertape_reader.stop()
        self.papertape_punch.stop()

        # Close Tkinter root window
        if self.root is not None:
            self.root.destroy()
        self.root = None

        if self._sounds is not None and hasattr(self._sounds, "stop"):
            self._sounds.stop()

        pygame.quit()

        if self._backend is not None and hasattr(self._backend, "close"):
            self._backend.close()


# -------------------------
# Simple test harness
# -------------------------

if __name__ == "__main__":
    _config = ASR33Config("ASR-33 Pygame Frontend")
    cfg_data = _config.get_merged_config()
    # Initialize selections as None placeholders first.
    #pylint: disable=invalid-name
    comm_backend_selection = None
    data_throttle = None
    term_selection = None
    my_frontend = None

    # Comm backend selection: SSHV2 or Serial
    backend_type = cfg_data.backend.get("type", default="serial")
    if backend_type == "ssh":
        comm_backend_selection = SSHV2Backend(
            upper_layer=None,
            config=cfg_data.backend.ssh_config
        )
    elif backend_type == "serial": # Serial backend
        comm_backend_selection = SerialBackend(
            upper_layer=None,
            config=cfg_data.backend.serial_config
        )
    else:
        raise ValueError(f"Unsupported backend type: {backend_type}")

    # Comm backend feeds data to DataThrottle, which feeds data to Terminal
    data_throttle = DataThrottle(
        lower_layer=comm_backend_selection,
        upper_layer=None,
        config=cfg_data.data_throttle.config
    )

    term_selection = Terminal(
        comm_interface=data_throttle,
        frontend=None,
        config=cfg_data.terminal.config
    )

    # ASR-33 sound support
    sound_selection = ASR33_Sounds()

    # Run the frontend
    frontend_selection = ASR33PygameFrontend(
        term_selection,
        data_throttle,
        config=cfg_data,
        sound=sound_selection,
    )

    # Assign layers that are forward referenced
    comm_backend_selection.upper_layer = data_throttle
    data_throttle.upper_layer = term_selection
    term_selection.frontend = frontend_selection

    frontend_selection.run()
