#!/usr/bin/env python3

"""Simulated papertape reader and punch front-end components
   for ASR-33 terminal emulator.
"""

import os
import threading
import time
from typing import BinaryIO
from tkinter import filedialog
from asr33_pt_animate_tk import PapertapeViewer

# Default configuration constants

# if True, the tape reader will automatically skip past all leading
# 000 bytes at the start of the tape when the reader is turned on.
READER_AUTO_SKIP_LEADING_NULLS = True

# if True, sets msb to 1 on all bytes read from tape. This is useful
# If you created an ASCII tape file with an editor on a modern system
# for use on systems that expect 7-bit ASCII with mark parity like some
# versions of OS/8. This is also useful when creating FOCAL-69 source tapes.
READER_SET_MSB = False

class HexViewer:
    "Hex dumper front-end component (streaming one byte at a time)"
    def __init__(self):
        self.offset = 0

    def dump_byte(self, byte_data: bytes):
        "Dump byte to the console in hex format, 16 per line"

        for byte in byte_data:
            # Print offset at the start of each line
            if self.offset % 16 == 0:
                print(f'{self.offset:08X}  ', end='')

            # Print the byte in hex format
            print(f'{byte:02X} ', end='')

            self.offset += 1

            # End the line after 16 bytes
            if self.offset % 16 == 0:
                print()

PAPER_TAPE_VIEWER_SCALE = 150
MAX_VIEWER_ROWS = 1024

class PapertapeReader():
    "Papertape reader front-end component"

    def __init__(self, master, backend, config):
        self.master = master
        self.backend = backend
        self.tape_loaded = False
        self.init_window_pos = True
        self.active = False  # initially stopped
        self.stop_cause = ""
        self.pt_name_path = None
        self.init_name_path = config.get("initial_file_path", default=None)
        self.tape_data = b''
        self.position = 0
        self.trailing_o000_idx = None
        self.trailing_o200_idx = None
        self.skip_leading_nulls = config.get(
            "skip_leading_nulls",
            default=READER_AUTO_SKIP_LEADING_NULLS
        )
        self.set_msb = config.get(
            "set_msb",
            default=READER_SET_MSB
        )
        self.parent_x = 200
        self.parent_y = 500
        self.parent_h = 600

        self.papertape_viewer = PapertapeViewer(
            outer=self,
            master=self.master,
            mode="reader",
            config=config,
            window_title="Papertape Reader",
            scale=PAPER_TAPE_VIEWER_SCALE,
            max_rows=config.get("max_rows", default=MAX_VIEWER_ROWS),
            x_org=100,
            y_org=100,
            height=100
        )

        if self.papertape_viewer is None:
            raise RuntimeError("Could not create papertape reader viewer")

        # Threading attributes
        self.thread_running = False
        self._thread = threading.Thread(target=self._tape_reader_worker, daemon=True)
        self._start_thread()

    def _start_thread(self) -> None:
        """Start the papertape reader worker thread."""
        if not self.thread_running:
            self.thread_running = True
            self._thread.start()

    def _stop_thread(self) -> None:
        """Stop the papertape reader worker thread."""
        self.thread_running = False
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def close_viewer_event(self) -> None:
        "Close the papertape reader"
        self.unload_tape()
        if self.papertape_viewer is not None:
            self.papertape_viewer.withdraw()
        if self.master is not None:
            self.master.after_idle(self.master.focus_force)

    def _end_check(self, position: int) -> bool:
        if position >= len(self.tape_data):
            self.stop_cause = "end_of_tape"
            return True

        # If there is no viewer or autostop is disabled, do not auto-stop.
        if self.papertape_viewer is None or not getattr(self.papertape_viewer, "autostop", False):
            self.stop_cause = ""
            return False

        # Check for autostop trailing o200 and o000 bytes if enabled
        if self.trailing_o200_idx is not None:
            if position > self.trailing_o200_idx:
                self.stop_cause = "trailing_o200"
                return True
            return False

        if self.trailing_o000_idx is not None:
            if position > self.trailing_o000_idx:
                self.stop_cause = "trailing_o000"
                return True
        return False

    def _tape_reader_worker(self) -> None:
        "Perform papertape reading background tasks"
        while self.thread_running:
            sleep_time = 0.050  # 50mS default sleep time
            if self.active:
                if self._end_check(self.position):
                    self.active = False
                    if self.papertape_viewer is not None:
                        self.papertape_viewer.set_to_off_state()
                    continue

                data_byte = bytes(self.tape_data[self.position:self.position+1])
                if self.set_msb:
                    data_byte = bytes([data_byte[0] | 0x80])  # set msb

                # Send 8-bit byte directly to backend for transmission
                if self.backend is not None:
                    self.backend.send_data(data_byte)

                if self.papertape_viewer is not None:
                    self.papertape_viewer.add_byte(data_byte)

                self.position += 1
                sleep_time = 0.003 # Faster reading speed while data is available

            time.sleep(sleep_time)

    def _load_tapefile(self, name_path: str) -> None:
        with open(name_path, 'br') as f:
            # if a tape is already loaded, remove it
            if self.tape_loaded:
                self.unload_tape()

            # Read the new tape file
            self.tape_data = f.read()

            # Locate first 0o200 and 0o000 trailer bytes at end of file data
            n = len(self.tape_data)
            i = n - 1
            # count consecutive trailing 0o000 bytes
            while i >= 0 and self.tape_data[i] == 0o000:
                i -= 1
            self.trailing_o000_idx = i + 1 if i < n - 1 else None

            # Then count consecutive 0o200 immediately before that
            j = i
            while j >= 0 and self.tape_data[j] == 0o200:
                j -= 1
            self.trailing_o200_idx = j + 1 if j < i else None

            self.position = 0
            self.tape_loaded = True
            self.active = False  # initially stopped

    def stop(self) -> None:
        "Shutdown the papertape reader viewer"
        self.unload_tape()
        if self.papertape_viewer is not None:
            self.papertape_viewer.close()
            self.papertape_viewer = None
        self._stop_thread()

    def process(self) -> None:
        "Process viewer's enqueued data."
        if self.papertape_viewer is not None:
            self.papertape_viewer.process_viewer(self.tape_loaded)
        self._update_file_status()

    def active_status(self) -> bool:
        "Return true if papertape reader is active"
        return self.active

    def show(self, parent_x=100, parent_y=100, parent_h=500) -> None:
        "Show the papertape reader viewer"
        if self.papertape_viewer is None:
            return

        self.parent_x = parent_x
        self.parent_y = parent_y
        self.parent_h = parent_h

        if self.init_window_pos:
            # Set initial window position
            xoffset = 10  # pixels gap
            child_w = self.papertape_viewer.winfo_width()  # keep current width
            child_h = self.parent_h // 2 # half the height of parent
            # Position at the bottom left of parent window
            child_x = self.parent_x - child_w - xoffset
            child_x = max(10, child_x)
            child_y = self.parent_y
            self.papertape_viewer.geometry(f"{child_w}x{child_h}+{child_x}+{child_y}")
            self.init_window_pos = False
            self.papertape_viewer.transient(self.master)

        self.papertape_viewer.deiconify()
        self._update_file_status()

    def hide(self) -> None:
        "Hide the papertape reader viewer"
        if self.papertape_viewer is None:
            return
        self.papertape_viewer.withdraw()
        if self.master is not None:
            self.master.after_idle(self.master.focus_force)

    def on(self) -> bool:
        "Turn on the papertape reader"
        if self.papertape_viewer is None:
            return False
        if not self.tape_loaded:
            return False

        # Auto-skip leading nulls if enabled
        if self.skip_leading_nulls:
            n = len(self.tape_data)
            while self.position < n and self.tape_data[self.position] == 0o000:
                self.position += 1

        self.active = True
        self.stop_cause = ""
        return True

    def off(self) -> bool:
        "Turn off the papertape reader"
        if self.papertape_viewer is None:
            return False
        self.active = False
        return True

    def rewind_tape(self) -> None:
        "Rewind the papertape to the beginning"
        if self.tape_loaded and not self.active and self.position>0:
            self.position = 0  # Reset tape read position to the beginning.
            if self.papertape_viewer is not None:
                self.papertape_viewer.unload_tape()

    def load_tape(self) -> str:
        "Load a tape file"
        if self.papertape_viewer is None:
            return "error"

        if self.active:
            self.stop()  # if active, turn off reader
        saved_active = self.active

        initial_dir = os.path.dirname(self.init_name_path) if self.init_name_path else "."
        name_path = get_reader_file_selection(self.papertape_viewer, initial_dir=initial_dir)

        if name_path is None:
            self.active = saved_active
            return "cancelled"
        try:
            self._load_tapefile(name_path)
        except (FileNotFoundError, PermissionError, OSError) as e:
            print(f"Could not load tape file: {e}")
            self.active = saved_active
            return "error"
        self.pt_name_path = name_path
        self.init_name_path = name_path
        self._update_file_status()
        return "loaded"

    def unload_tape(self) -> bool:
        "Remove tape from reader"
        if self.papertape_viewer is None:
            return False
        self.off()  #ensure reader is stopped
        if self.tape_loaded:
            self.tape_loaded = False
            self.pt_name_path = None
        self.papertape_viewer.unload_tape()
        self.tape_data = b''
        self.position = 0
        self._update_file_status()
        return True

    def _update_file_status(self) -> None:
        """Update the file status in the papertape viewer."""
        if self.papertape_viewer is None:
            return

        if self.pt_name_path is None:
            fileinfo = "Unloaded"
            status = ""
        else:
            filename = os.path.basename(self.pt_name_path)
            filelen = len(self.tape_data)
            fileinfo = f"{filename} ({filelen} bytes)"
            percent = ((self.position / filelen) * 100) if filelen > 0 else 0
            if self.active:
                status = f"{percent:.1f}% read"
            elif self.stop_cause == "trailing_o200":
                status = "Stopped: auto-stop (200)"
            elif self.stop_cause == "trailing_o000":
                status = "Stopped: auto-stop (null)"
            elif self.stop_cause == "end_of_tape":
                status = f"{percent:.1f}% read (end)"
            else:
                status = f"{percent:.1f}% read"

        self.papertape_viewer.set_file_status(
            f"File: {fileinfo}", f"{status}")


class PapertapePunch():
    "Papertape punch front-end component"

    def __init__(self, master, config):
        self.master = master
        self.tape_loaded = False
        self.init_window_pos = True
        self.active = False  # initially stopped
        self.tape_file = None
        self.pt_name_path = None
        self.init_name_path = config.get("initial_file_path", default=None)
        self.file_write_mode = config.get("mode", default="overwrite")
        self.parent_x = 200
        self.parent_y = 200
        self.parent_h = 600

        self.papertape_viewer = PapertapeViewer(
            outer=self,
            master=self.master,
            mode="punch",
            config=config,
            window_title="Papertape Punch",
            scale=PAPER_TAPE_VIEWER_SCALE,
            max_rows=config.get("max_rows", default=MAX_VIEWER_ROWS),
            x_org=100,
            y_org=100,
            height=100
        )

        if self.papertape_viewer is None:
            raise RuntimeError("Could not create papertape reader viewer")

    def close_viewer_event(self) -> None:
        "Close the papertape reader"    
        self.unload_tape()
        if self.papertape_viewer is not None:
            self.papertape_viewer.withdraw()
        if self.master is not None:
            self.master.after_idle(self.master.focus_force)

    def process(self):
        "Process viewer's enqueued data and update the display."
        if self.papertape_viewer is not None:
            self.papertape_viewer.process_viewer(self.tape_loaded)
        self._update_file_status()

    def stop(self) -> None:
        "Shutdown the papertape punch viewer"
        self.unload_tape()
        if self.papertape_viewer is not None:
            self.papertape_viewer.close()
            self.papertape_viewer = None

    def show(self, parent_x=100, parent_y=100, parent_h=500) -> None:
        "Show the papertape reader viewer"
        if self.papertape_viewer is None:
            return

        self.parent_x = parent_x
        self.parent_y = parent_y
        self.parent_h = parent_h

        if self.init_window_pos:
            # Set initial window position
            xoffset = 10  # pixels gap
            child_w = self.papertape_viewer.winfo_width()  # keep current width
            child_h = self.parent_h // 2 # half the height of parent
            # Position at the bottom left of parent window
            child_x = self.parent_x - child_w - xoffset
            child_x = max(10, child_x)
            child_y = self.parent_y
            self.papertape_viewer.geometry(f"{child_w}x{child_h}+{child_x}+{child_y}")
            self.init_window_pos = False
            self.papertape_viewer.update_idletasks()
            self.papertape_viewer.transient(self.master)

        self.papertape_viewer.deiconify()
        self._update_file_status()

    def hide(self) -> None:
        "Hide the papertape reader viewer"
        if self.papertape_viewer is None:
            return
        self.papertape_viewer.withdraw()
        if self.master is not None:
            self.master.after_idle(self.master.focus_force)

    def toggle_file_write_mode(self) -> None:
        "Toggle the papertape punch file write mode"
        self.file_write_mode = (
            "append" if self.file_write_mode == "overwrite" else "overwrite"
        )
        self._update_file_status()

    def on(self) -> bool:
        "Turn on the papertape punch"
        if self.papertape_viewer is None:
            return False
        if self.tape_file is None:
            return False  # No tape file not open
        self.active = True  # Enable punching
        return True

    def off(self) -> bool:
        "Turn off the papertape punch but keep tape loaded"
        if self.papertape_viewer is None:
            return False
        self.active = False  # Disable punching
        return True

    def load_tape(self) -> str:
        "Load a tape file for punching"
        if self.papertape_viewer is None:
            return "error"

        # if a tape file is already open, close it
        if self.tape_file is not None:
            self.tape_file.close()

        initial_dir = os.path.dirname(self.init_name_path) if self.init_name_path else "."
        name_path = get_reader_file_selection(self.papertape_viewer, initial_dir=initial_dir)

        if name_path is None:
            return "canceled"
        try:
            if self.file_write_mode == "append":
                self.tape_file = self._open_for_append_with_preview(
                    name_path,
                    self.papertape_viewer
                )
            else:
                self.tape_file = open(name_path, "wb")  # overwrite mode

            self.tape_file.seek(0, os.SEEK_END)
        except (FileNotFoundError, PermissionError, OSError) as _:
            return "error"

        self.pt_name_path = name_path
        self.init_name_path = name_path
        self.tape_loaded = True
        self.active = False  # initially stopped
        self._update_file_status()
        return "loaded"

    def unload_tape(self) -> bool:
        "Remove tape from punch"
        if self.papertape_viewer is None:
            return False
        self.off()
        if self.tape_loaded:
            self.tape_loaded = False
        if self.tape_file is not None:
            self.tape_file.close()  # Close tape file if open
            self.pt_name_path = None
        self.papertape_viewer.unload_tape()
        self._update_file_status()
        return True

    def punch_bytes(self, data: str | bytes) -> None:
        """Punch one or more bytes onto the tape."""
        if self.papertape_viewer is None:
            return
        if not self.active or self.tape_file is None:
            return

        if isinstance(data, str):
            bytes_data = data.encode("ascii")
        else:
            bytes_data = data

        self.tape_file.write(bytes_data)
        self.papertape_viewer.add_byte(bytes_data)
        self._update_file_status()

    def _open_for_append_with_preview(self, filename, pt_viewer) -> BinaryIO:
        "Open tape file for appending, and load existing contents into viewer"
        file = open(filename, "ab+")  # open for read and append
        file.seek(0)
        contents = file.read()  # read existing contents
        pt_viewer.add_byte(contents)  # load existing contents into viewer
        return file  # caller must close this

    def _update_file_status(self) -> None:
        """Update the file status in the papertape viewer."""
        if self.papertape_viewer is None:
            return

        mode = self.file_write_mode.capitalize()
        if self.pt_name_path is None:
            filename = "Unloaded"
            status = f"Mode: {mode}"
        else:
            filename = os.path.basename(self.pt_name_path)
            mode = self.file_write_mode.capitalize()
            file_size = self.tape_file.tell() if self.tape_file is not None else 0
            status = f"{mode}, {file_size} bytes"

        self.papertape_viewer.set_file_status(f"File: {filename}", f"{status}")


def get_file_types():
    """Return file types for the file dialog."""
    return [
        ("Tape files", ("*.pt", "*.pb", "*.pa", "*.pr", "*.bpt", "*.apt", "*.rpt", "*.tap")),
        ("Source files", ("*.pa", "*.ba", "*.ft", "*.fc", "*.tx")),
        ("Misc files", ("*.raw", "*.asc", "*.s19", "*.S29", "*.srec")),
        ("All files", "*.*")
    ]

def get_reader_file_selection(master, initial_dir="."):
    """Open a file dialog for selecting a reader file."""
    filename = filedialog.askopenfilename(
        parent=master,
        title="Select file for paper tape reader to read",
        initialdir=initial_dir,
        filetypes=get_file_types()
    )
    # Restore focus to parent
    if master is not None:
        master.after_idle(master.focus_force)
    return filename if filename else None

def get_punch_file_selection(master, initial_dir="."):
    """Open a file dialog for selecting a punch file."""
    filename = filedialog.asksaveasfilename(
        parent=master,
        title="Select file for paper tape punch to write",
        initialdir=initial_dir,
        filetypes=get_file_types(),
        confirmoverwrite=False,
        defaultextension=".pt"
    )
    # Restore focus to parent
    if master is not None:
        master.after_idle(master.focus_force)
    return filename if filename else None
