#!/usr/bin/env python3

"""
ASR-33 Frontend using Tkinter (Canvas text renderer with overstrike support).

- Uses Tk font metrics for cell sizing.
- Overstrike is supported: each strike in a cell is drawn as a separate text item.
- Much faster than PIL-based rendering.
"""

import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont
import os
import sys
import ctypes
import subprocess
import pathlib
import shutil
from typing import Any, cast
from fontTools.ttLib import TTFont, TTLibError

from asr33_config import ASR33Config
from asr33_backend_ssh import SSHV2Backend
from asr33_backend_serial import SerialBackend
from asr33_terminal import Terminal
from asr33_shim_throttle import DataThrottle
from asr33_sounds_sm import ASR33AudioModule as ASR33_Sounds
from asr33_papertape import PapertapeReader, PapertapePunch

# True for true ASR-33 emulation.
# False if you want to allow lowercase input from keyboard.
KEYBOARD_UPPERCASE_ONLY = False

# Controls bit-7 generation. Options are ("mark", "space", "even")
# "mark" seems to be the best choice for compatibility with DEC programs.
KEYBOARD_PARITY_MODE = "space"

# True to send carriage return to host on startup to wake it up
SEND_CR_AT_STARTUP = False

# Default terminal layout constants
MARGIN = 15
COLUMNS = 72
ROWS = 20
PAGE_SCROLL_STEP = 12
MOUSE_SCROLL_STEP = 3

TEXT_COLOR = "#555555"
PAPER_COLOR = "#ffeedd"

FONT_PATH = "Teletype33.ttf"
FONT_SIZE = 20

# Maximum characters per second when pasting from clipboard. Set to 0 for unlimited.
PASTE_MAX_CPS = 300
# Tick interval (ms) used to drain the paste queue.
PASTE_TICK_MS = 5


# Cross-platform font registration
def register_font(ttf_path: str) -> bool:
    """Register a TTF font with the OS for use by Tkinter."""
    if sys.platform.startswith("win"):
        fr_private  = 0x10
        res = ctypes.windll.gdi32.AddFontResourceExW(ttf_path, fr_private, 0)
        return res > 0
    elif sys.platform.startswith("linux"):
        fonts_dir = pathlib.Path.home() / ".local/share/fonts"
        fonts_dir.mkdir(parents=True, exist_ok=True)
        dest = fonts_dir / os.path.basename(ttf_path)
        try:
            if not dest.exists():
                shutil.copy(ttf_path, dest)
                subprocess.run(["fc-cache", "-fv"], check=True)
            return True
        except (FileNotFoundError, shutil.SameFileError,
                PermissionError, subprocess.CalledProcessError) as e:
            print("Font registration failed:", e)
            return False
    else:
        print("Unsupported platform")
        return False


def get_ttf_family_name(ttf_path) -> str | None:
    """Attempts to read the family name from a TTF file, handling common errors."""
    try:
        font = TTFont(ttf_path)
        # Narrow the table to Any so static type checkers don't complain
        name_table = cast(Any, font['name'])
        # Use getattr with a safe default in case the table implementation
        # differs in some environments or stubs.
        for record in getattr(name_table, "names", []):
            if getattr(record, "nameID", None) == 1:
                try:
                    if hasattr(record, "toUnicode"):
                        return record.toUnicode()
                    # Fallback method if the primary method fails due to bad encoding
                    return getattr(record, "string", b"").decode('utf-16-be', errors='ignore')
                except (UnicodeDecodeError, TypeError):
                    return getattr(record, "string", b"").decode('utf-16-be', errors='ignore')
    except (FileNotFoundError, PermissionError, OSError, TTLibError) as _:
        # Catch specific OS errors and the fontTools library errors
        pass
    except Exception as e:
        # Catch truly unexpected errors, log them, and re-raise them
        print(f"An unexpected error occurred: {e}")
        raise

    return None


class ASR33TkFrontend:
    """ Tkinter-based frontend for ASR-33 terminal emulation.
    """
    def __init__(self, terminal, backend, config, sound=None):
        self._term = terminal
        self._backend = backend
        self.cfg = config

        # sounds
        self._sounds = sound
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

        # Register font before creating Tk root
        family_name = get_ttf_family_name(FONT_PATH)
        if family_name:
            if register_font(FONT_PATH):
                pass
#                print("Registered font family:", family_name)
            else:
                print("Warning: Unable to register font: ", family_name)
                family_name = ""
        else:
            print("Warning: font not found:", FONT_PATH)

        self.root = tk.Tk()
        self.root.resizable(False, False)
        self.root.title(f"ASR-33 Emulator using {self._backend.get_info_string()}")
        self.display_update_needed = False

        if not family_name:
            family_name = "DejaVu Sans Mono"  # fallback font
            print("Using fallback font:", family_name)

        self.tk_font = tkfont.Font(
            family=family_name,
            size=FONT_SIZE,
            weight="normal",
            slant="roman"
        )

        actual = self.tk_font.actual()
        if actual["family"] != family_name:
            raise RuntimeError("ERROR: Could not load font family:", family_name)

        self.tape_running_state = False
        self.papertape_reader = PapertapeReader(
            master=self.root,
            backend=self._backend,
            config=self.cfg.tape_reader.config
        )
        self.papertape_punch = PapertapePunch(
            master=self.root,
            config=self.cfg.tape_punch.config
        )

        # Character dimensions
        self.font_w = self.tk_font.measure("X")
        # Set height to match ASR-33 aspect ratio of 10 CPI and 6 LPI
        self.font_h = self.font_w * 10 // 6
        # Compute content size
        self.content_width = self.font_w * self._term.width
        self.content_height = self.font_h * self._term.height + 1

        # Frame with background
        self.frame = tk.Frame(self.root, bg=PAPER_COLOR)
        self.frame.pack(expand=True, fill="both")

        # Container for canvas + vertical scrollbar
        self.canvas_frame = tk.Frame(self.frame, bg=PAPER_COLOR)
        self.canvas_frame.pack(padx=MARGIN, pady=MARGIN, fill=tk.BOTH, expand=True)

        # Canvas for displaying text
        self.canvas = tk.Canvas(
            self.canvas_frame,
            bg=PAPER_COLOR,
            highlightthickness=0,
            borderwidth=0,
            relief=tk.FLAT,
            width=self.content_width,
            height=self.content_height
        )

        self.vscroll = ttk.Scrollbar(
            self.canvas_frame,
            orient="vertical",
            style="TScrollbar",
            command=self._on_scrollbar
        )

        self.status_font = tkfont.Font(
            family="New Courier",
            size=10,
            weight="bold",
            slant="roman"
        )

        # Derived smaller font used for status bar items to avoid passing tuples mixing
        # a Font instance with a size (which static type checkers reject).
        self.status_bar_font = tkfont.Font(
            family=self.status_font.actual().get("family", "New Courier"),
            size=int(FONT_SIZE * 0.55),
            weight=self.status_font.actual().get("weight", "bold"),
            slant=self.status_font.actual().get("slant", "roman")
        )

        # Create status area along the bottom
        self.status_area = tk.Frame(self.frame, bd=1, relief=tk.FLAT, bg=PAPER_COLOR)
        for i in range(1,4):
            self.status_area.grid_columnconfigure(i, weight=1)

        self._data_rate_status_bar, self._data_rate_status_label, self._data_rate_status_button = (
            self.create_status_bar(
                parent_frame=self.status_area,
                status_text=f"Data Rate: {self._data_rate.capitalize()}",
                status_text_width=15,
                button_text="Unthrottle" if self._data_rate == "throttled" else "Throttle",
                button_command=self._throttle_button_command,
            )
        )
        self._data_rate_status_bar.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)

        self._mute_status_bar, self._mute_status_label, self._mute_status_button = (
            self.create_status_bar(
                parent_frame=self.status_area,
                status_text=f"Sound: {self._sound_mute_state.capitalize()}",
                status_text_width=15,
                button_text="Mute" if self._sound_mute_state == "unmuted" else "Unmute",
                button_command=self._mute_button_command,
            )
        )
        self._mute_status_bar.grid(row=0, column=1, sticky="nsew", padx=2, pady=2)

        self._lid_status_bar, self._lid_status_label, self._lid_status_button = (
            self.create_status_bar(
                parent_frame=self.status_area,
                status_text=f"Lid: {self._lid_state.capitalize()}",
                status_text_width=12,
                button_text="Lower Lid" if self._lid_state == "up" else "Raise Lid",
                button_command=self._lid_button_command,
            )
        )
        self._lid_status_bar.grid(row=0, column=2, sticky="nsew", padx=(5, 2), pady=2)

        self._loopback_status_bar, self._loopback_status_label, self._loopback_status_button = (
            self.create_status_bar(
                parent_frame=self.status_area,
                status_text=f"Comm Status: {self._loopback_state.capitalize()}",
                status_text_width=15,
                button_text="Local" if self._loopback_state == "line" else "Line",
                button_command=self._loopback_button_command,
            )
        )
        self._loopback_status_bar.grid(row=0, column=3, sticky="nsew", padx=(2, 5), pady=2)
        self.status_area.update_idletasks()  # ensure correct height measurement

        self._printer_status_bar, self._printer_status_label, self._printer_status_button = (
            self.create_status_bar(
                parent_frame=self.status_area,
                status_text=f"Printer: {self._printer_state.capitalize()}",
                status_text_width=12,
                button_text="Off" if self._printer_state == "on" else "On",
                button_command=self._printer_button_command,
            )
        )
        self._printer_status_bar.grid(row=0, column=4, sticky="nsew", padx=(2, 5), pady=2)
        self.status_area.update_idletasks()  # ensure correct height measurement

        # Pack canvas left, scrollbar right
        self.canvas.pack(side="left", fill=tk.BOTH, expand=True)
        self.vscroll.pack(side="right", fill="y")
        self.status_area.pack(side="bottom", fill="x", expand=False)

        self.cursor_id = self.canvas.create_rectangle(0, 0, self.font_w, self.font_h)

        # Size the window to exactly fit canvas + margins + scrollbar width
        # Use a default scrollbar width of 20 px to avoid depending on winfo_reqwidth timing
        scrollbar_width = self.vscroll.winfo_reqwidth() or 20
        status_area_height = self.status_area.winfo_reqheight()
        total_w = self.content_width + scrollbar_width + 2 * MARGIN
        total_h = self.content_height + status_area_height + 2 * MARGIN
        self.root.geometry(f"{total_w}x{total_h}")
        self.root.minsize(total_w, total_h)

        # Pre-create text items for each cell
        self.text_ids = []
        self.shadow = [[" "] * self._term.width for _ in range(self._term.height)]

        for r in range(self._term.height):
            row_ids = []
            y = r * self.font_h
            for c in range(self._term.width):
                x = c * self.font_w
                tid = self.canvas.create_text(
                    x, y,
                    text=" ",
                    anchor="nw",
                    font=self.tk_font,
                    fill=TEXT_COLOR
                )
                row_ids.append(tid)
            self.text_ids.append(row_ids)

        # Status overlay label
        self.status = tk.Label(
            self.canvas_frame,
            text="VIEWING HISTORY",
            bg=TEXT_COLOR,
            fg=PAPER_COLOR,
            font=self.status_font,
            padx=5,
            pady=2
        )
        self.status.place(x=self.canvas.winfo_width(), y=0, anchor="ne")
        self.status.lower()  # hidden initially

        # Scroll state
        self.screen_top_lln = None  # None means at bottom
        self.overstrike_enabled = True

        # Bind keys
        self.root.bind("<Key>", self._keypress)
        self.root.bind_all("<F1>", self._reader_show_f1)
        self.root.bind_all("<F2>", self._reader_hide_f2)
        self.root.bind_all("<F3>", self._punch_show_f3)
        self.root.bind_all("<F4>", self._punch_hide_f4)
        self.root.bind_all("<F5>", self._throttle_toggle_f5)
        self.root.bind_all("<F6>", self._sound_toggle_mute_f6)
        self.root.bind_all("<F7>", self._sound_lid_toggle_f7)
        self.root.bind_all("<F8>", self._loopback_toggle_f8)
        self.root.bind_all("<F9>", self._printer_toggle_f9)
        self.root.bind("<Prior>", self._page_up)
        self.root.bind("<Next>", self._page_down)
        self.root.bind("<Home>", self._page_home)
        self.root.bind("<End>", self._page_end)
        # Cross-platform mouse wheel support:
        # - Windows/macOS: Tk reports '<MouseWheel>' with event.delta
        # - X11 (Linux): Tk reports Button-4 (wheel up) and Button-5 (wheel down)
        self.root.bind("<MouseWheel>", self._mouse_scroll)
        self.root.bind("<Button-4>", self._mouse_scroll)
        self.root.bind("<Button-5>", self._mouse_scroll)
        # Initial draw + periodic update
        self._update_display()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if self.send_cr_at_startup:
            self._backend.send_data(b'\r') # send CR to wake up host

    def create_status_bar(
            self,
            parent_frame,
            status_text,
            status_text_width,
            button_text,
            button_command,
        ) -> tuple[tk.Frame, tk.Label, tk.Button]:
        """
        Creates a single custom status bar (Frame) with a Label and a Button.
        Returns the frame and the label for easy updates.
        """
        # Use a sunken relief for a traditional status bar look
        frame = tk.Frame(
            parent_frame,
            bg=PAPER_COLOR,
            bd=1,
            relief=tk.SUNKEN
        )

        # Label for status text
        # anchor="w" aligns the text to the west (left) within its allocated space
        label = tk.Label(
            frame,
            bg=PAPER_COLOR,
            font=self.status_bar_font,
            text=status_text,
            anchor="w",
            padx=5,
            width=status_text_width
        )
        label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Button on the right
        button = tk.Button(
            frame,
            bg=PAPER_COLOR,
            font=self.status_bar_font,
            text=button_text,
            width=8
        )
        button.config(command=lambda btn=button: button_command(btn))
        button.pack(side=tk.RIGHT, padx=5, pady=2)

        return frame, label, button


    def _lid_button_command(self, button_widget):
        if self._lid_state == "up":
            self._lid_state = "down"
            new_button_text = "Raise Lid"
        else:
            self._lid_state = "up"
            new_button_text = "Lower Lid"
        self._lid_status_label.config(text=f"Lid: {self._lid_state.title()}")
        button_widget.config(text=new_button_text)
        self._sound_manage_lid()

    def _mute_button_command(self, button_widget):
        if self._sound_mute_state == "unmuted":
            self._sound_mute_state = "muted"
            new_button_text = "Unmute"
        else:
            self._sound_mute_state = "unmuted"
            new_button_text = "Mute"
        self._mute_status_label.config(text=f"Sound: {self._sound_mute_state.title()}")
        button_widget.config(text=new_button_text)
        self._sound_manage_mute()

    def _throttle_button_command(self, button_widget):
        if self._data_rate == "throttled":
            self._data_rate = "unthrottled"
            new_button_text = "Throttle"
        else:
            self._data_rate = "throttled"
            new_button_text = "Unthrottle"
        self._data_rate_status_label.config(text=f"Data Rate: {self._data_rate.title()}")
        button_widget.config(text=new_button_text)
        self._manage_throttle()

    def _loopback_button_command(self, button_widget):
        if self._loopback_state == "line":
            self._loopback_state = "local"
            new_button_text = "Line"
        else:
            self._loopback_state = "line"
            new_button_text = "Local"
        self._loopback_status_label.config(text=f"Comm Status: {self._loopback_state.title()}")
        button_widget.config(text=new_button_text)
        self._manage_loopback()

    def _printer_button_command(self, button_widget):
        if self._printer_state == "on":
            self._printer_state = "off"
            new_button_text = "On"
        else:
            self._printer_state = "on"
            new_button_text = "Off"
        self._printer_status_label.config(text=f"Printer: {self._printer_state.title()}")
        button_widget.config(text=new_button_text)
        self._manage_printer()

    def _on_close(self):
        # destroy both the frontend window and the root
        if self.root is not None:
            self.root.quit()

    # pylint: disable=unused-argument
    # --- Papertape controls ---
    def _reader_show_f1(self, event=None):
        if self.root is None:
            return
        self.root.update_idletasks()  # ensure geometry is up to date
        yoffset = 0
        if sys.platform.startswith("win"):
            yoffset = 0
        if sys.platform.startswith("linux"):
            yoffset = -38
        elif sys.platform.startswith("darwin"):
            yoffset = -28

        self.papertape_reader.show(
            parent_x=self.root.winfo_x(),
            parent_y=self.root.winfo_y() + yoffset + self.root.winfo_height()//2,
            parent_h=self.root.winfo_height()
        )
        return "break"

    def _reader_hide_f2(self, event=None):
        self.papertape_reader.hide()
        return "break"

    def _punch_show_f3(self, event=None):
        if self.root is None:
            return
        self.root.update_idletasks()  # ensure geometry is up to date
        yoffset = 0
        if sys.platform.startswith("linux"):
            yoffset = -38
        self.papertape_punch.show(
            parent_x=self.root.winfo_x(),
            parent_y=self.root.winfo_y()+ yoffset,
            parent_h=self.root.winfo_height()
        )
        return "break"

    def _punch_hide_f4(self, event=None):
        self.papertape_punch.hide()
        return "break"

    def _throttle_toggle_f5(self, event=None):
        self._throttle_button_command(self._data_rate_status_button)
        return "break"

    def _sound_toggle_mute_f6(self, event=None):
        self._mute_button_command(self._mute_status_button)
        return "break"

    def _sound_lid_toggle_f7(self, event=None):
        self._lid_button_command(self._lid_status_button)
        return "break"

    def _loopback_toggle_f8(self, event=None):
        self._loopback_button_command(self._loopback_status_button)
        return "break"

    def _printer_toggle_f9(self, event=None):
        self._printer_button_command(self._printer_status_button)
        return "break"

    def _sound_manage_lid(self):
        if self._sounds is not None and hasattr(self._sounds, "lid"):
            lid_up = self._lid_state == "up"
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

    def _page_up(self, event=None):
        """ scroll up method """
        self._scroll_helper(-PAGE_SCROLL_STEP)
        self.display_update_needed = True
        return "break"

    def _page_down(self, event=None):
        """ scroll down method """
        self._scroll_helper(PAGE_SCROLL_STEP)
        self.display_update_needed = True
        return "break"

    def _page_home(self, event=None):
        self.screen_top_lln = self._term.line_history.top_lln()
        self.display_update_needed = True
        return "break"

    def _page_end(self, event=None):
        self.screen_top_lln = None
        self.display_update_needed = True
        return "break"

    def _keypress(self, event):
        ch = event.char
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
            return "break"
        return

    def _mouse_scroll(self, event):
        """Handle mouse wheel scrolling (cross-platform).

        Accept both X11 Button-4/5 events (event.num) and
        Windows/macOS '<MouseWheel>' events (event.delta).
        """
        # X11 mouse wheel: event.num == 4 (up), 5 (down)
        num = getattr(event, 'num', None)
        if num is not None:
            if num == 4:
                self._scroll_helper(-MOUSE_SCROLL_STEP)
            elif num == 5:
                self._scroll_helper(MOUSE_SCROLL_STEP)
        else:
            # Windows / macOS: event.delta > 0 => up
            delta = getattr(event, 'delta', 0)
            if delta > 0:
                self._scroll_helper(-MOUSE_SCROLL_STEP)
            else:
                self._scroll_helper(MOUSE_SCROLL_STEP)

        self.display_update_needed = True

    def _on_scrollbar(self, *args):
        """Scrollbar callback. Accepts 'moveto' and 'scroll' commands."""
        total = len(self._term.line_history)
        height = self._term.height

        if total <= height:
            # Nothing to scroll
            self.screen_top_lln = None
            self.display_update_needed = True
            return

        cmd = args[0]
        if cmd == 'moveto':
            try:
                frac = float(args[1])
            except (IndexError, ValueError):
                return
            start_index = int(round(frac * total))
            start_index = max(0, min(start_index, total - height))
            # Map start_index back to logical line number
            self.screen_top_lln = self._term.line_history.top_lln() + start_index
            # If start_index is at bottom, set to None
            if start_index >= total - height:
                self.screen_top_lln = None
            self.display_update_needed = True

        elif cmd == 'scroll':
            try:
                count = int(args[1])
                what = args[2]
            except ValueError as _:
                return
            if what == 'units':
                # units = lines
                self._scroll_helper(count)
            elif what == 'pages':
                self._scroll_helper(count * height)
            self.display_update_needed = True

    def _update_scrollbar(self):
        """Update the scrollbar thumb to reflect current view in history."""
        total = len(self._term.line_history)
        height = self._term.height
        if total <= height:
            # Full range
            try:
                self.vscroll.set(0.0, 1.0)
            except (tk.TclError, AttributeError):
                pass
            return

        # Determine start index (0-based) relative to history top
        if self.screen_top_lln is None:
            start_index = max(0, total - height)
        else:
            start_index = self.screen_top_lln - self._term.line_history.top_lln()
            start_index = max(0, min(start_index, total - height))

        start_frac = start_index / float(total)
        end_frac = (start_index + height) / float(total)
        try:
            self.vscroll.set(start_frac, end_frac)
        except (tk.TclError, AttributeError, ValueError):
            pass

    # --- Rendering ---
    def _draw_cursor(self):
        """Draw the cursor rectangle"""
        # Get cursor position in logical (terminal) coords
        cursor_x, cursor_y = self._term.get_cursor_position()

        # Determine physical (visible) start line in logical coords
        # Use the same visible-start calculation as other view helpers
        # Convert cursor logical row to index in history, then to visible offset
        top_lln = self._term.line_history.top_lln()
        cursor_index = cursor_y - top_lln
        start_index = self._visible_start_lln()

        x0 = cursor_x * self.font_w
        y0 = (cursor_index - start_index) * self.font_h
        x1 = x0 + self.font_w
        y1 = y0 + self.font_h
        self.canvas.coords(self.cursor_id, (x0, y0, x1, y1))

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
        # Return list of line objects by 0-based index into history
        return [
            self._term.line_history.get_line(i)
            for i in range(start_index, end_index)
        ]

    def _get_cell_coords(self, r, c):
        """Calculate the pixel coordinates for the top-left corner of a cell."""
        x = c * self.font_w
        y = r * self.font_h
        return x, y

    def _visible_start_lln(self):
        """Return 0-based start index corresponding to visible[0]."""
        total = len(self._term.line_history)
        if self.screen_top_lln is None:
            start_index = max(0, total - self._term.height)
        else:
            start_index = min(
                self.screen_top_lln - self._term.line_history.top_lln(),
                max(0, total - self._term.height)
            )
            start_index = max(0, start_index)
        # Return 0-based start index into line_history.lines (not a logical lln)
        return start_index

    def _update_display(self):
        """Update the terminal display based on the current scroll position."""
        visible = self._get_visible_lines()

        # Clear out old "extra" items first before drawing new ones
        self.canvas.delete("overstrike_extra")

        for r, line_obj in enumerate(visible):
            for c in range(self._term.width):
                stack = line_obj.get_strike_stack(c)

                # If the stack is empty, default to space
                if not stack:
                    ch = " "
                    extras = []
                else:
                    # The top character is always the primary display item
                    ch = stack[-1]
                    # The remaining are the base/overstrike layers
                    extras = stack[:-1]

                # --- 1. Update the primary (top-most) character ---
                if ch != self.shadow[r][c]:
                    self.shadow[r][c] = ch
                    # Ensure the primary item is drawn last (highest Z-order)
                    self.canvas.itemconfig(self.text_ids[r][c], text=ch)

                if self.overstrike_enabled is True:
                    # --- 2. Dynamically draw the overstrike characters ---
                    if extras:
                        # Get the X, Y coordinates for this specific grid cell
                        x_coord, y_coord = self._get_cell_coords(r, c)

                        # Iterate through the base characters and draw them dynamically
                        for base_ch in extras:
                            # Create a new text item for each overstruck character
                            # Assign it a unique tag ("overstrike_extra") so we can
                            #  delete all of them next cycle
                            self.canvas.create_text(
                                x_coord, y_coord,
                                text=base_ch,
                                anchor='nw',
                                tags=("overstrike_extra", f"cell_{r}_{c}"),
                                font=self.tk_font,
                                fill=TEXT_COLOR
                            )

        # Update scrollbar thumb and draw cursor on screen
        try:
            self._update_scrollbar()
        except (AttributeError, tk.TclError):
            # Non-fatal: if scrollbar isn't available yet, ignore
            pass
        self._draw_cursor()

        # Status overlay visibility
        status_x = self.canvas_frame.winfo_width() - self.vscroll.winfo_width()
        self.status.place(x=status_x, y=0, anchor="ne")
        if self.screen_top_lln is not None and len(self._term.line_history) > self._term.height:
            if self.screen_top_lln == self._term.line_history.top_lln():
                self.status.config(text="VIEWING HISTORY: TOP")
            else:
                self.status.config(text="VIEWING HISTORY")
            self.status.lift()  # show
        else:
            self.status.lower()  # hide


    def _periodic_tasks(self):
        """Periodic tasks: request data, update display if needed."""
        # Schedule the actual work to run when Tk is idle
        def work():
            if self.display_update_needed:
                self._update_display()
                self.display_update_needed = False

            # play sounds for new characters
            while self._term.sound_queue_len() > 0:
                ch, col = self._term.pop_char_from_sound_queue()
                if self._sounds is not None and hasattr(self._sounds, "print_char"):
                    self._sounds.print_char(ch)
                if col == 62:
                    if self._sounds is not None and hasattr(self._sounds, "column_bell"):
                        self._sounds.column_bell()

            if  self.papertape_reader is not None:
                for _ in range(50 if self._data_rate == "unthrottled" else 1):
                    self.papertape_reader.process()

            if self._sounds is not None and hasattr(self._sounds, "tape_reader_running"):
                tape_running_status = self.papertape_reader.active_status()
                # track state changes to avoid redundant calls
                if self.tape_running_state != tape_running_status:
                    self.tape_running_state = tape_running_status
                    self._sounds.tape_reader_running(tape_running_status)

            self.papertape_punch.process()

        if self.root is not None:
            self.root.after_idle(work)

        # Re‑schedule this loop to tick again in 10mS
        if self.root is not None:
            self.root.after(20, self._periodic_tasks)

    def receive_data(self, data: bytes):
        """Handle incoming terminal data."""
        self.papertape_punch.punch_bytes(data)
        self.display_update_needed = True

    def check_focus(self):
        """Ensure the main window has focus."""
        if self.root is None:
            return
        w = self.root.focus_get()
        print("Focus is on:", w)
        self.root.after(1000, self.check_focus)

    def run(self):
        """Run the Tkinter main loop."""

        # Initial sound, backend and printer state management
        self._sound_manage_lid()
        self._sound_manage_mute()
        self._manage_throttle()
        self._manage_loopback()
        self._manage_printer()

        if not self._backend is None and hasattr(self._backend, "start"):
            self._backend.start()

        if self._sounds and hasattr(self._sounds, "start"):
            self._sounds.start()

        if self.root is not None:
            self.root.after(20, self._periodic_tasks)
            self.root.mainloop()

        # Cleanup on exit
        self.papertape_reader.stop()
        self.papertape_punch.stop()

        # Close Tkinter root window
        if self.root is not None:
            self.root.destroy()
        self.root = None

        if self._sounds is not None and hasattr(self._sounds, "stop"):
            self._sounds.stop()

        if self._backend is not None and hasattr(self._backend, "close"):
            self._backend.close()


# Simple test harness to run the frontend outside of wrapper
if __name__ == "__main__":
    _config = ASR33Config("ASR-33 Tkinter Frontend")
    cfg_data = _config.get_merged_config()
    # Initialize selections as None placeholders first.
    #pylint: disable=invalid-name
    comm_backend_selection = None
    data_throttle = None
    term_selection = None
    frontend_selection = None

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
    frontend_selection = ASR33TkFrontend(
        term_selection,
        data_throttle,
        config=cfg_data,
        sound=sound_selection
    )

    # Assign layers that are forward referenced
    comm_backend_selection.upper_layer = data_throttle
    data_throttle.upper_layer = term_selection
    term_selection.frontend = frontend_selection

    frontend_selection.run()
