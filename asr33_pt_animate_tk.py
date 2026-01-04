#!/usr/bin/env python3

"""ASR-33 style papertape viewer using Tkinter with row image caching."""
import queue
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont
from PIL import Image, ImageDraw, ImageFont, ImageTk

# --- Default Configuration Constants ---
PAPER_COLOR = "#ffeedd"
UNPUNCHED_GHOST_OUTLINE = True
ASCII_CHAR_MASK_MSB = True
BIT_LABEL_BASE = 0  # 0: zero-based (0-7), 1: one-based (1-8)
MAX_LABEL_WIDTH = 20  # Maximum width for status labels

class PapertapeViewer(tk.Toplevel):
    """ASR-33 style papertape viewer using Tkinter with row image caching."""
    # pylint: disable=too-many-instance-attributes, too-many-statements, too-many-locals
    def __init__(
            self,
            outer,
            master,
            mode: str,
            config,
            window_title=None,
            scale=100,
            max_rows=20,
            x_org=100,
            y_org=100,
            height=200,
    ) -> None:
        super().__init__(master)
        self.outer = outer
        self._master = master
        self.mode = mode
        self._safe_off_request = False
        self.withdraw()
        self.resizable(False, False)
        self.title(window_title)
        self.protocol("WM_DELETE_WINDOW", self._handle_close_event)
        self.incoming_queue = queue.Queue()
        self.file_write_mode = config.get("mode", default="overwrite")
        self.max_rows = config.get("max_rows", default=max_rows)
        self.row_counter = 0
        self.mirrored = False
        self.autostop = config.get("auto_stop", default=False)
        self.cb_autostop = tk.IntVar(value=self.autostop)
        self.unpunched_ghost_outline = config.get(
            "ghost_outline",
            default=UNPUNCHED_GHOST_OUTLINE
        )
        self.bit_label_base = config.get("bit_label_base", default=BIT_LABEL_BASE)
        self.ascii_char_mask_msb = config.get(
            "ascii_char_mask_msb",
            default=ASCII_CHAR_MASK_MSB
        )

        # geometry
        self.scale = scale
        self.x_org = x_org
        self.y_org = y_org
        self.height = height
        self.hole_pitch_pix_x = 0.100 * scale
        self.hole_pitch_pix_y = 0.100 * scale
        self.bit_radius_pix = (0.072 * scale) / 2
        self.sprocket_radius_pix = (0.046 * scale) / 2
        self.first_bit_pitch_pix_x = 0.092 * scale
        self.tape_canvas_margin_pix = self.hole_pitch_pix_y
        self.right_tape_edge_pix_x = self.tape_canvas_margin_pix + 1.000 * scale

        self.top_punch_row_pix_y = self.tape_canvas_margin_pix * 1.5

        # hole columns (fixed relative x positions from left edge of tape
        # 9 columns including sprocket)
        self.cols_x = [
            self.first_bit_pitch_pix_x
            + i * self.hole_pitch_pix_x for i in range(9)
        ]

        # Maps bit index (0-7) to physical column index (0-8, excluding SP index)
        self.unmirrored_col_map = {0: 0, 1: 1, 2: 2, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8}
        self.mirrored_col_map = {7: 0, 6: 1, 5: 2, 4: 3, 3: 4, 2: 6, 1: 7, 0: 8}

        # Sprocket column index
        self.unmirrored_sprocket_col = 3
        self.mirrored_sprocket_col = len(self.cols_x) - 1 - self.unmirrored_sprocket_col

        self.col_map = self.unmirrored_col_map
        self.sprocket_col = self.unmirrored_sprocket_col

        self.text_x = self.right_tape_edge_pix_x

        # font setup
        self.font_size = int(0.1 * self.scale)
        self.header_font_size = int(0.07 * self.scale)
        pil_mono_candidates = [
            "consola.ttf", "Consolas.ttf", "Courier New.ttf",
            "cour.ttf", "DejaVuSansMono.ttf", "LiberationMono-Regular.ttf",
        ]
        self.font = None
        for fname in pil_mono_candidates:
            try:
                self.font = ImageFont.truetype(fname, self.font_size)
                break
            except OSError:
                continue
        if self.font is None:
            self.font = ImageFont.load_default()

        # tkinter font for header labels (use a fixed-width family)
        try:
            self.header_tkfont = tkfont.Font(family="Courier", size=self.header_font_size)
        except tk.TclError:
            # fallback to default named font if creation fails
            self.header_tkfont = tkfont.nametofont("TkFixedFont")
            self.header_tkfont.configure(size=self.header_font_size)

        # canvas width estimate
        sample_numeric = "0xFF 0o377"
        numeric_text_w = int(self.font.getlength(sample_numeric))
        pad = max(2, int(self.hole_pitch_pix_x * 0.15))
        img2_w = int(numeric_text_w + pad * 2)
        rotated_w = int(self.hole_pitch_pix_x)
        gap_pixels = int(max(4, self.hole_pitch_pix_x * 0.35))
        required_right = self.text_x + rotated_w / 2 + gap_pixels + img2_w
        self.tape_canvas_width = int(required_right + self.tape_canvas_margin_pix)

        # UI Setup
        self._setup_ui()
        self.update_idletasks()
        self.update()

        # Position tape viewer
        if self.x_org is not None:
            child_x = 9999  # Offscreen initially
            child_y = 9999
            self.geometry(f"+{child_x}+{child_y}")

        self.rows = []
        self.punched_rows_pix_y = 0

        # tape outline rectangle
        self.outline_id = self.tape_canvas.create_rectangle(
            10, 10, 10, 10, outline="#999999"
        )
        self._canvas_configure(0)
        self._update_tape_outline()
        self._row_image_cache = {}

    def _setup_ui(self) -> None:
        """Sets up the control frame, fixed header, and scrollable canvas."""
        self.configure(bg=PAPER_COLOR)

        # Control frame containing buttons and status labels
        control_frame = tk.Frame(
            self,
            bd=0,
            bg=PAPER_COLOR,
            relief=tk.FLAT,
            highlightthickness=0
        )
        control_frame.pack(side="top", fill="x", padx=10, pady=2)

        self.bt_load = tk.Button(
            control_frame,
            command=self._on_load,
            bg=PAPER_COLOR,
            width=6,
            state="normal",
            text="Load"
        )

        self.bt_unload = tk.Button(
            control_frame,
            bg=PAPER_COLOR,
            command=self.unload_click,
            width=6,
            state="disabled",
            text="Unload"
        )

        self.bt_on = tk.Button(
            control_frame,
            bg=PAPER_COLOR,
            command=self.on_button_click,
            width=6,
            state="disabled",
            text="Start" if self.mode == "reader" else "On",
        )

        self.bt_off = tk.Button(
            control_frame,
            bg=PAPER_COLOR,
            command=self.off_button_click,
            state="disabled",
            text="Stop" if self.mode == "reader" else "Off",
            width=6,
        )

        self.bt_on.grid(row=0, column=0, padx=2, pady=2, sticky="w")
        self.bt_off.grid(row=1, column=0, padx=2, pady=2, sticky="w")
        self.bt_load.grid(row=0, column=1, padx=2, pady=2, sticky="w")
        self.bt_unload.grid(row=1, column=1, padx=2, pady=2, sticky="w")

        self.file_status1 = tk.StringVar(value="Line 1")
        self.status_label1 = tk.Label(
            control_frame,
            bg=PAPER_COLOR,
            textvariable=self.file_status1, anchor="w",
            width=MAX_LABEL_WIDTH,
            relief=tk.GROOVE
        )
        self.status_label1.grid(row=0, column=2, columnspan=2, sticky="ew", padx=(5, 2), pady=2)

        self.file_status2 = tk.StringVar(value="Line 2")
        self.status_label2 = tk.Label(
            control_frame,
            bg=PAPER_COLOR,
            textvariable=self.file_status2, anchor="w",
            width=MAX_LABEL_WIDTH,
            relief=tk.GROOVE
        )
        self.status_label2.grid(row=1, column=3, sticky="ew", padx=(5, 2), pady=2)

        if self.mode == "reader":
            self.bt_rewind = tk.Button(
                control_frame,
                bg=PAPER_COLOR,
                command=self._on_rewind,
                state="disabled",
                text="Rewind",
                width=6,
            )
            self.bt_rewind.grid(row=1, column=2, padx=2, pady=2, sticky="w")
        else:
            self.bt_mode = tk.Button(
                control_frame,
                bg=PAPER_COLOR,
                command=self._on_mode,
                state="normal",
                text="Mode",
                width=6,
            )
            self.bt_mode.grid(row=1, column=2, padx=2, pady=2, sticky="w")

        control_frame.grid_columnconfigure(3, weight=1)

        # Fixed top frame for tape bit numbers header
        header_frame = tk.Frame(
            self,
            bg=PAPER_COLOR,
            bd=0,
            relief=tk.FLAT,
            highlightthickness=0
        )
        header_frame.pack(side="top", fill="x", padx=10, pady=(2, 0))

        # Initialize header canvas
        header_canvas_height = int(self.hole_pitch_pix_y)
        self.header_canvas = tk.Canvas(
            header_frame,
            bg=PAPER_COLOR,
            width=self.text_x,
            height=header_canvas_height,
            bd=0,
            relief=tk.FLAT,
            highlightthickness=0
        )
        self.header_canvas.grid(row=0, column=0, sticky="sw")
        self._draw_bit_numbers()

        if self.mode == "reader":
            self.chk_autostop = tk.Checkbutton(
                header_frame,
                bg=PAPER_COLOR,
                text="Auto stop",
                variable=self.cb_autostop,
                command=self._on_autostop_changed
            )
            self.chk_autostop.grid(row=0, column=3, padx=(10,0), sticky="w")

        # Clicking the header canvas mirrors tape display
        self.header_canvas.bind("<Button-1>", self._toggle_mirror_display)

        # Frame for tape display canvas and vertical scrollbar
        tape_frame = tk.Frame(self, bg=PAPER_COLOR, bd=0, relief=tk.FLAT)
        tape_frame.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 2))

        # Scrollable tape image canvas
        self.tape_canvas = tk.Canvas(
            tape_frame,
            bg=PAPER_COLOR,
            width=self.tape_canvas_width,
            height=self.height,
            bd=0,
            relief=tk.FLAT,
            highlightthickness=0
        )
        self.scrollbar = ttk.Scrollbar(
            tape_frame,
            orient="vertical",
            style="TScrollbar",
            command=self.tape_canvas.yview
        )
        self.scrollbar.pack(side="right", fill="y")
        self.tape_canvas.pack(side="left", fill="both", expand=True)
        self.tape_canvas.configure(yscrollcommand=self.scrollbar.set)

        # Bind mouse wheel and keys for scrolling (cross-platform)
        # - Windows/macOS: '<MouseWheel>' with event.delta
        # - X11 (Linux): '<Button-4>' (wheel up) and '<Button-5>' (wheel down)
        self.tape_canvas.bind("<MouseWheel>", self._mousewheel_handler)
        self.tape_canvas.bind("<Button-4>", self._mousewheel_handler)
        self.tape_canvas.bind("<Button-5>", self._mousewheel_handler)
        self.bind("<Home>", lambda e: self.tape_canvas.yview_moveto(0))
        self.bind("<End>", lambda e: self.tape_canvas.yview_moveto(1))
        self.bind("<Next>", lambda e: self.tape_canvas.yview_scroll(1, "pages"))
        self.bind("<Prior>", lambda e: self.tape_canvas.yview_scroll(-1, "pages"))

        # Bind keyd for alternate On/Off (aka stop/start) control
        if self.mode == "reader":
            self.bind("<KeyPress-1>", lambda e: self.on_button_click())
            self.bind("<KeyPress-2>", lambda e: self.off_button_click())
        else:
            self.bind("<KeyPress-3>", lambda e: self.on_button_click())
            self.bind("<KeyPress-4>", lambda e: self.off_button_click())

    def _draw_bit_numbers(self) -> None:
        """Draws the fixed bit numbers (0-7) and ASCII/HEX header text."""
        self.header_canvas.delete("bit_numbers")
        # Center Y for the row of numbers, adjusted slightly lower to clear the top margin
        center_y_bit_num = int(self.hole_pitch_pix_y / 2)
        if self.mirrored:
            if self.bit_label_base == 0:
                header_labels = [7, 6, 5, 4, 3, "S", 2, 1, 0]
            else:
                header_labels = [8, 7, 6, 5, 4, "S", 3, 2, 1]
        else:
            if self.bit_label_base == 0:
                header_labels = [0, 1, 2, "S", 3, 4, 5, 6, 7]
            else:
                header_labels = [1, 2, 3, "S", 4, 5, 6, 7, 8]
        # Draw the bits-label header
        for i, label in enumerate(header_labels):
            center_x = self.tape_canvas_margin_pix + self.cols_x[i] - 1
            self.header_canvas.create_text(
                center_x, center_y_bit_num,
                text=str(label),
                font=self.header_tkfont,
                fill="black",
                justify="center",
                tags=("bit_numbers", "fixed_text")
            )

    def _toggle_mirror_display(self, event) -> None:
        """Toggle the mirrored state and redraw everything (Horizontal only)."""
        # pylint: disable=unused-argument
        self.mirrored = not self.mirrored
        # Update column map and sprocket position
        if self.mirrored:
            self.col_map = self.mirrored_col_map
            self.sprocket_col = self.mirrored_sprocket_col
        else:
            self.col_map = self.unmirrored_col_map
            self.sprocket_col = self.unmirrored_sprocket_col
        # Update bit number header
        self._draw_bit_numbers()
        # Clear the row image cache
        self._row_image_cache.clear()
        # Redraw all tape rows on the scrollable canvas (Fixed: no vertical flip)
        if self.tape_canvas and self.tape_canvas.winfo_exists():
            self.tape_canvas.delete("row")

            # Iterate from newest row (index 0) to oldest, and draw them starting
            # from the top of the canvas, moving down.
            for i, (byte, row_tag) in enumerate(self.rows):
                # Calculate Y coordinate: Top Y + (index * row_height)
                y = self.top_punch_row_pix_y + (i * self.hole_pitch_pix_y)
                self._draw_row(byte, y, row_tag)

    def _mousewheel_handler(self, event) -> None:
        """Handle mouse wheel scrolling (cross-platform).

        Accept both X11 Button-4/5 events (event.num) and
        Windows/macOS '<MouseWheel>' events (event.delta).
        """
        num = getattr(event, 'num', None)
        direction = 0
        if num is not None and num in (4, 5):
            # X11: Button-4 = wheel up, Button-5 = wheel down
            if num == 4:
                direction = 1
            elif num == 5:
                direction = -1
        else:
            delta = getattr(event, 'delta', 0)
            direction = 1 if delta > 0 else -1

        self.tape_canvas.scan_mark(0, 0)
        self.tape_canvas.scan_dragto(0, int(direction * self.hole_pitch_pix_y), gain=1)

    def _on_autostop_changed(self) -> None:
        """Handles the Auto Stop checkbox change event."""
        self.autostop = self.cb_autostop.get()

    # --- Control panel button callbacks ---
    def _on_rewind(self) -> None:
        """Handles the Load/rewind button click event."""
        if self.bt_rewind["state"] == "disabled":
            return
        self.outer.rewind_tape()

    def _on_load(self) -> None:
        """Handles the Load/rewind button click event."""
        if self.bt_load["state"] == "disabled":
            return
        result = self.outer.load_tape()
        if result == "loaded":
            self.set_button_state("load")

    def unload_click(self) -> None:
        """Handles the Unload button click event."""
        if self.bt_unload["state"] == "disabled":
            return
        if self.bt_unload["text"] == "Unload":
            if self.outer.unload_tape():
                self.set_button_state("unload")

    def _on_mode(self) -> None:
        """Handles the Mode button click event."""
        if self.bt_mode["state"] == "disabled":
            return
        if self.outer.toggle_file_write_mode():
            self.set_button_state("mode")

    def on_button_click(self) -> None:
        """Handles the On/Start button click event."""
        if self.bt_on["state"] == "disabled":
            return
        if self.outer.on():
            self.set_button_state("on")

    def off_button_click(self) -> None:
        """Handles the Off/Stop button click event."""
        if self.bt_off["state"] == "disabled":
            return
        if self.outer.off():
            self.set_button_state("off")

    def _handle_close_event(self) :
        if self.outer.close_viewer_event:
            self.outer.close_viewer_event()

    def set_button_state(self, new_state: str) -> None:
        """Sets the state of the control buttons based on the operational state."""
        if self.mode == "reader":
            self.set_button_state_reader(new_state)
        else:
            self.set_button_state_punch(new_state)

    def set_button_state_reader(self, new_state: str) -> None:
        """Sets the state of the reader buttons."""
        if new_state == "on":
            self.bt_on.config(state="disabled")
            self.bt_off.config(state="normal")
            self.bt_load.config(state="disabled")
            self.bt_unload.config(state="disabled")
            self.bt_rewind.config(state="disabled")
        elif new_state == "off":
            self.bt_on.config(state="normal")
            self.bt_off.config(state="disabled")
            self.bt_load.config(state="normal")
            self.bt_unload.config(state="normal")
            self.bt_rewind.config(state="normal")
        elif new_state == "load":
            self.bt_on.config(state="normal")
            self.bt_off.config(state="disabled")
            self.bt_unload.config(state="normal")
            self.bt_load.config(state="normal")
            self.bt_rewind.config(state="disabled")
        elif new_state == "unload":
            self.bt_on.config(state="disabled")
            self.bt_off.config(state="disabled")
            self.bt_load.config(state="normal")
            self.bt_unload.config(state="disabled")
            self.bt_rewind.config(state="disabled")
        else:
            print(f"PapertapeViewer: set_button_state_reader: unknown button state: '{new_state}'")

    def set_button_state_punch(self, new_state: str) -> None:
        """Sets the state of the punch buttons."""
        if new_state == "on":
            self.bt_on.config(state="disabled")
            self.bt_off.config(state="normal")
            self.bt_load.config(state="disabled")
            self.bt_unload.config(state="disabled")
            self.bt_mode.config(state="disabled")
        elif new_state == "off":
            self.bt_on.config(state="normal")
            self.bt_off.config(state="disabled")
            self.bt_load.config(state="disabled")
            self.bt_unload.config(state="normal")
            self.bt_mode.config(state="disabled")
        elif new_state == "load":
            self.bt_on.config(state="normal")
            self.bt_off.config(state="disabled")
            self.bt_load.config(state="disabled")
            self.bt_unload.config(state="normal")
            self.bt_mode.config(state="disabled")
        elif new_state == "unload":
            self.bt_on.config(state="disabled")
            self.bt_off.config(state="disabled")
            self.bt_load.config(state="normal")
            self.bt_unload.config(state="disabled")
            self.bt_mode.config(state="normal")
        else:
            print(f"PapertapeViewer: set_button_state_punch: unknown button state: '{new_state}'")

    def close(self) -> None:
        """Closes the viewer and removes the tape."""
        self.unload_tape()

    def set_file_status(self, file_status1: str, file_status2: str = "") -> None:
        """Updates the status display lines."""
        self.file_status1.set(file_status1)
        if file_status2 is not None:
            self.file_status2.set(file_status2)

    def add_byte(self, byte_data: bytes) -> None:
        """Adds bytes to the incoming queue for display processing."""
        if not byte_data:
            return
        for byte_val in byte_data:
            self.incoming_queue.put(byte_val)

    def process_viewer(self, tape_loaded: bool) -> None:
        """
        Processes incoming data from the queue and handles safe shutdown requests.
        """
        if self._safe_off_request:
            self._safe_off_request = False
            self.off_button_click()
        if self.state() != "normal":
            return
        while not self.incoming_queue.empty():
            byte = self.incoming_queue.get()
            if tape_loaded:
                self._process_byte(byte)

    def unload_tape(self) -> None:
        """Clears all displayed tape data and resets viewer state."""
        while not self.incoming_queue.empty():
            self.incoming_queue.get()
        if self.tape_canvas and self.tape_canvas.winfo_exists():
            self.tape_canvas.delete("row")
        self.rows.clear()
        self._canvas_configure(0)
        self._update_tape_outline()
        self.set_button_state("unload")

    def set_to_off_state(self) -> None:
        """Sets a flag to safely request the viewer to stop reading/punching."""
        self._safe_off_request = True

    def _update_tape_outline(self) -> None:
        """Updates the visible outline box around the punched tape area."""
        x1 = self.tape_canvas_margin_pix  # T
        y1 = self.top_punch_row_pix_y - self.hole_pitch_pix_y
        x2 = self.right_tape_edge_pix_x
        y2 = self.top_punch_row_pix_y + self.punched_rows_pix_y
        self.tape_canvas.coords(self.outline_id, x1, y1, x2, y2)

    def _process_byte(self, byte) -> None:
        """Process a single byte: add to viewer."""
        if not self.tape_canvas.winfo_exists():
            return

        # Scroll existing rows down by one row height (move them visually down)
        self.tape_canvas.move("row", 0, self.hole_pitch_pix_y)

        row_tag = f"row_{self.row_counter}"
        self.row_counter += 1

        # Draw the new row at the top position (0 index in row array)
        self._draw_row(byte, self.top_punch_row_pix_y, row_tag)
        self.rows.insert(0, (byte, row_tag)) # Newest row at index 0

        # Maintain max_rows
        if len(self.rows) > self.max_rows:
            _, old_tag = self.rows.pop()
            self.tape_canvas.delete(old_tag)

        self._canvas_configure(len(self.rows))
        self._update_tape_outline()

    def _canvas_configure(self, row_count: int) -> None:
        """Sets the scrollable region based on the number of rows."""
        self.punched_rows_pix_y = row_count * self.hole_pitch_pix_y
        w = self.tape_canvas_width
        h = self.tape_canvas_margin_pix * 2 + self.punched_rows_pix_y - 1
        self.tape_canvas.configure(scrollregion=(0, 0, w, h))

    def _get_row_image(self, byte) -> ImageTk.PhotoImage:
        """Return a cached PhotoImage for the given byte, creating it if needed."""
        # pylint: disable=too-many-branches
        cache_key = (byte, self.mirrored)
        if cache_key in self._row_image_cache:
            return self._row_image_cache[cache_key]

        # Cached image not found, create new row image
        row_h = int(self.hole_pitch_pix_y)
        row_w = self.tape_canvas_width
        img = Image.new("RGBA", (row_w, row_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        ghost_outline = "#c0c0c0"
        punch_fill = "#3b3b3b"
        punch_outline = "#aaaaaa"

        y = row_h // 2

        # Draw sprocket hole (uses the current self.sprocket_col)
        x = int(self.cols_x[self.sprocket_col])
        r = int(self.sprocket_radius_pix)
        draw.ellipse((x - r, y - r, x + r, y + r), outline=ghost_outline, width=1)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=punch_fill, outline=punch_outline, width=1)

        # 2. Data bit holes (bits 0..7 mapped through col_map)
        for bit in range(8):
            col_index = self.col_map[bit]
            x = int(self.cols_x[col_index])
            r = int(self.bit_radius_pix)

            if self.unpunched_ghost_outline or ((byte >> bit) & 1):
                draw.ellipse((x - r, y - r, x + r, y + r), outline=ghost_outline, width=1)
            if (byte >> bit) & 1:
                draw.ellipse(
                    (x - r, y - r, x + r, y + r),
                    fill=punch_fill,
                    outline=punch_outline,
                    width=1
                )

        # 3. ASCII glyph (rotated)
        chrval = byte
        if self.ascii_char_mask_msb:
            chrval &= 0x7F
        ch = chr(chrval) if 32 <= chrval < 127 else "·"
        glyph_img = Image.new("RGBA", (row_h, row_h), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glyph_img)
        gdraw.text((row_h / 2, row_h / 2), ch, font=self.font, fill="black", anchor="mm")
        rotated = glyph_img.rotate(90, expand=True)

        # 4. Numeric labels
        hex_text = f"0x{byte:02X} 0o{byte:03o}"
        gap_pixels = int(max(4, self.hole_pitch_pix_x * 0.35))

        # Position the ASCII glyph
        glyph_center_x = int(self.text_x)
        img.paste(
            rotated,
            (int(glyph_center_x - rotated.width // 2), int(y - rotated.height // 2)),
            rotated
        )

        # Position the Hex/Oct text
        hex_oct_text_x = glyph_center_x + rotated.width // 2 + gap_pixels
        draw.text((hex_oct_text_x, y - self.font_size // 2), hex_text,
                  font=self.font, fill="black")

        tk_img = ImageTk.PhotoImage(img)
        self._row_image_cache[cache_key] = tk_img
        return tk_img

    def _draw_row(self, byte, y, row_tag) -> None:
        """Draw one tape row image at y with a tag."""
        tk_img = self._get_row_image(byte)
        x = self.tape_canvas_margin_pix
        self.tape_canvas.create_image(
            x, y,
            image=tk_img,
            anchor="w",
            tags=(row_tag, "row")
        )

# End of PapertapeViewer class

# Simple test harness to exercise the PapertapeViewer functionality
class TestHarness:
    """Test application to demonstrate the PapertapeViewer functionality."""

    def __init__(self):
        """ Initializes the test application. """
        self.root = None

    def load_tape(self):
        """ Simulates loading a tape. """
        print("Load tape button clicked.")
        return "loaded"

    def unload_tape(self):
        """ Simulates removing a tape. """
        print("Unload tape button clicked.")
        return True

    def on(self):
        """ Simulates turning the papertape reader on. """
        print("On/Start button clicked.")
        return True

    def off(self):
        """ Simulates turning the papertape reader off. """
        print("Off/Stop button clicked.")
        return True

    def rewind_tape(self):
        """ Simulates rewinding the tape. """
        print("Reader mode rewind button clicked.")
        return True

    def close_viewer_event(self):
        """ Handles the viewer close event. """
        print("Viewer close event triggered.")
        if self.root is not None:
            self.root.quit()

    def toggle_file_write_mode(self):
        """ Simulates toggling punch mode. """
        print("Punch mode toggle clicked.")
        return True

    def run(self):
        """ Runs the test application. """
        self.root = tk.Tk()
        self.root.withdraw() # Hide the main root window

        # 2. Create the viewer instance
        viewer = PapertapeViewer(
            outer=self,
            master=self.root,
#            mode="reader",
            mode="punch",
            config={"max_rows": 20, "auto_stop": False, "mode": "overwrite"},
            window_title="ASR-33 Papertape Viewer (Test)",
            height=300,
            scale=150,
            max_rows=20,
        )
        viewer.deiconify() # Show the viewer window

        # 3. Simulate initial tape load and power on
#        viewer.load_rewind_button_click()
#        viewer.on_button_click()
        viewer.set_file_status("File: DEMO.TXT", "Status: Ready to Read")

        # 4. Define demo tape data (bytes to simulate reading)
        # 0x00: NUL (All holes punched) - used as leader/trailer
        # 0x0D: CR (Carriage Return)
        # 0x0A: LF (Line Feed)
        # 0x20: SPACE
        # 0x48: H
        # 0x69: i
        # 0xFF: DEL (All bits punched in 8-bit mode)
        # 0x81: Bit 7 set (e.g., Extended ASCII/high bit test)

        demo_data = bytes([
            0x00, 0x00, 0x00, 0x0D, 0x0A, 0x20, 0x48, 0x65, 0x6C, 0x6C, 0x6F, 0x20,
            0x57, 0x6F, 0x72, 0x6C, 0x64, 0xFF, 0x81, 0x00, 0x0D, 0x0A, 0x00, 0x00,
            0x00, 0x0D, 0x0A, 0x54, 0x68, 0x69, 0x73, 0x20, 0x69, 0x73, 0x20, 0x61,
            0x20, 0x74, 0x65, 0x73, 0x74, 0x2E, 0x0D, 0x0A, 0x00, 0x00, 0x00

        ])

        # 5. Define a function to simulate reading data over time
        def simulate_read(data, index=0):
            """Simulates reading one byte at a time from the demo data."""
            if index < len(data):
                viewer.add_byte(data[index:index+1])
                viewer.process_viewer(tape_loaded=True)
                # Schedule the next byte read
                if self.root is not None:
                    self.root.after(20, simulate_read, data, index + 1)
            else:
                viewer.set_file_status("File: DEMO.TXT", "Status: Read Complete")
                viewer.off_button_click()

        # 6. Start the simulated read
        self.root.after(20, simulate_read, demo_data, 0)

        # 7. Start the Tkinter main loop
        self.root.mainloop()

if __name__ == "__main__":
    TestHarness().run()
