#!/usr/bin/env python3
"""
SSH V2 backend for ASR-33 emulator.
- Uses paramiko for SSH connections.
"""
import hashlib
import base64
import os
import threading
import time
import queue
from typing import Any
import socket
import paramiko
from paramiko.ssh_exception import SSHException, PasswordRequiredException, AuthenticationException

class HostKeyVerificationError(SSHException):
    """Raised when the SSH server's host key does not match the expected fingerprint."""
    pass #pylint: disable=unnecessary-pass

class SSHV2Backend:
    """SSH V2 backend for ASR-33 emulator."""
    def __init__(
        self,
        upper_layer: Any,
        config,
    ):
        self.upper_layer = upper_layer
        self.host = config.get("host", default="localhost")
        self.username = config.get("username", default="user")
        self.port = config.get("port", default=22)
        self.expected_fingerprint = config.get("expected_fingerprint", default=None)
        self.key_filename = config.get("key_filename", default=None)
        self.password = config.get("password", default=None)   # optional, can be None

        self._running = True
        self._input_queue = queue.Queue(maxsize=2048)

        self.transport = None
        self.channel = None
        self._rx_thread = None

        # Buffer and flag for password input
        self._buffer = ""
        self._waiting_for_password = False

    def start(self):
        """Start the SSH thread once the frontend is ready."""
        self._rx_thread = threading.Thread(target=self.ssh_thread, daemon=True)
        self._rx_thread.start()

    def send_data(self, data: bytes) -> None:
        """Send data received from upper layer to the SSH channel."""
        # Accept str, memoryview, bytearray and convert to bytes for consistent handling
        if isinstance(data, str):
            data = data.encode("ascii", "ignore")
        elif isinstance(data, memoryview):
            data = data.tobytes()
        elif isinstance(data, bytearray):
            data = bytes(data)

        if self.channel is not None:
            self.channel.send(data)
            return

        try:
            ch = data.decode("ascii", "ignore")
            # Process each character but do NOT echo character count while
            # waiting for password. Only echo the final newline when entered.
            masked_out = None
            for c in ch:
                if c in ("\r", "\n"):
                    try:
                        self._input_queue.put(self._buffer)
                    except queue.Full:
                        pass
                    self._buffer = ""
                    if self._waiting_for_password:
                        masked_out = b"\r\n"
                elif c in ("\x08", "\b", "\x7f"):
                    # Backspace / delete: remove last character if present
                    if self._buffer:
                        self._buffer = self._buffer[:-1]
                else:
                    self._buffer += c
        except (AttributeError, TypeError, UnicodeDecodeError):
            masked_out = None

        # Echo: if waiting for password, suppress per-character echoes and
        # only send newline on Enter; otherwise send raw data
        if self._waiting_for_password:
            if masked_out:
                self.upper_layer.receive_data(masked_out)
        else:
            self.upper_layer.receive_data(data)

    def keyboard_interactive_handler(self, title, instructions, prompt_list):
        """Respond to keyboard-interactive prompts from the SSH server."""
        responses = []
        if title:
            self.upper_layer.receive_data((title + "\r\n").encode("ascii", "ignore"))
        if instructions:
            self.upper_layer.receive_data((instructions + "\r\n").encode("ascii", "ignore"))

        for prompt, _ in prompt_list:
            self.upper_layer.receive_data((prompt + "\r\n").encode("ascii", "ignore"))
            # Wait for user input but remain interruptible so shutdown can occur
            while self._running:
                try:
                    response = self._input_queue.get(timeout=0.1)
                    responses.append(response)
                    break
                except queue.Empty:
                    continue
            else:
                raise SSHException("Shutdown while waiting for keyboard-interactive input")
        return responses

    def load_known_hosts(self):
        """Load known hosts from standard locations."""
        hostkeys = paramiko.HostKeys()
        paths = [
            os.path.expanduser("~/.ssh/known_hosts"),
            "/etc/ssh/ssh_known_hosts",
            "/etc/ssh/ssh_known_hosts2",
        ]
        for path in paths:
            try:
                hostkeys.load(path)
            except IOError:
                continue
        return hostkeys

    def verify_host_key_known_hosts(self, server_key):
        """Verify the server's host key against known_hosts."""
        hostkeys = self.load_known_hosts()
        host_patterns = [self.host, f"[{self.host}]:{self.port}"]

        for pattern in host_patterns:
            if pattern in hostkeys:
                for _, known_key in hostkeys[pattern].items():
                    if known_key == server_key:
                        return
                raise HostKeyVerificationError(f"Known host key mismatch for {self.host}")
        raise HostKeyVerificationError(f"Unknown host {self.host} (not in known_hosts)")

    def verify_explicit_fingerprint(self, server_key):
        """Verify the server's host key against an explicit expected fingerprint."""
        if not self.expected_fingerprint:
            return
        fp_raw = server_key.get_fingerprint()
        sha256_fp = "SHA256:" + base64.b64encode(hashlib.sha256(fp_raw).digest()).decode()
        if sha256_fp != self.expected_fingerprint:
            raise HostKeyVerificationError(
                f"Host key verification failed! Expected"
                f" {self.expected_fingerprint}, got {sha256_fp}"
            )

    def ssh_thread(self) -> None:
        """Background thread: manage SSH connection and data transfer."""
        time.sleep(1)  # brief delay to allow frontend to initialize
        try:
            sock = socket.create_connection((self.host, self.port), timeout=10)
            self.transport = paramiko.Transport(sock)
            self.transport.start_client(timeout=10)

            server_key = self.transport.get_remote_server_key()
            self.verify_host_key_known_hosts(server_key)
            self.verify_explicit_fingerprint(server_key)

            # --- Authentication sequence ---
            # 1. Try public key
            try:
                key_path = os.path.expanduser("~/.ssh/id_rsa")
                if os.path.exists(key_path):
                    pkey = paramiko.RSAKey.from_private_key_file(key_path)
                    self.transport.auth_publickey(self.username, pkey)
            except PasswordRequiredException:
                # Private key is encrypted / requires a passphrase — fall back to password auth
                pass
            except SSHException:
                # Paramiko SSH-related error while loading/using the key — skip publickey auth
                pass
            except (OSError, IOError):
                # File I/O errors reading the key file — skip publickey auth
                pass

            # 2. Always prompt for password if still not authenticated
            if not self.transport.is_authenticated():
                if not self.password:
                    prompt = f"Password for {self.username}@{self.host} on port {self.port}: "
                    self.upper_layer.receive_data(prompt.encode("ascii", "ignore"))
                    self._waiting_for_password = True
                    try:
                        # Wait for password input but allow shutdown to interrupt
                        while self._running:
                            try:
                                self.password = self._input_queue.get(timeout=0.1)
                                break
                            except queue.Empty:
                                continue
                        if not self._running:
                            return
                    finally:
                        self._waiting_for_password = False
                    self.upper_layer.receive_data(b"\r\n")  # Echo newline after password input
                try:
                    if self.password is None:
                        raise SSHException("No password provided for auth_password")
                    self.transport.auth_password(self.username, self.password)
                except (AuthenticationException, SSHException) as e:
                    self.upper_layer.receive_data(
                        f"Password authentication failed: {e}\r\n".encode("ascii", "ignore")
                    )
                finally:
                    self.password = None  # clear after use

            # 3. Try keyboard-interactive (for OTP/2FA)
            if not self.transport.is_authenticated():
                try:
                    self.transport.auth_interactive(
                        self.username,
                        self.keyboard_interactive_handler
                    )
                except (SSHException, AuthenticationException, OSError) as e:
                    self.upper_layer.receive_data(
                        f"Keyboard-interactive failed: {e}\r\n".encode("ascii", "ignore")
                    )

            if not self.transport.is_authenticated():
                raise SSHException("Authentication failed")

            # --- Shell setup ---
            self.channel = self.transport.open_session()
            self.channel.get_pty(term="tty33")
            self.channel.invoke_shell()

            while self._running and self.channel is not None and not self.channel.closed:
                if self.channel.recv_ready():
                    data = self.channel.recv(4096)
                    if data:
                        self.upper_layer.receive_data(data)
                else:
                    time.sleep(0.01)  # tiny sleep to avoid busy loop


        except socket.gaierror:
            self.upper_layer.receive_data(
                f"Network error: Unable to resolve or reach {self.host}. "
                f"Is the device offline?\r\n".encode("ascii", "ignore")
            )
        except socket.timeout:
            self.upper_layer.receive_data(
                f"Connection to {self.host}:{self.port} timed out.\r\n".encode("ascii", "ignore")
            )
        except ConnectionRefusedError:
            self.upper_layer.receive_data(
                f"Connection refused by {self.host}:{self.port}."
                f" Is SSH running?\r\n".encode("ascii", "ignore")
            )
        except HostKeyVerificationError as e:
            self.upper_layer.receive_data(f"SECURITY ERROR: {e}\r\n".encode("ascii", "ignore"))
        except SSHException as e:
            self.upper_layer.receive_data(f"SSH error: {e}\r\n".encode("ascii", "ignore"))
        except (OSError, RuntimeError, ValueError) as e:
            # Catch common runtime/file/network related errors without masking all exceptions
            self.upper_layer.receive_data(f"Unexpected error: {e}\r\n".encode("ascii", "ignore"))
        finally:
            try:
                if self.channel:
                    self.channel.close()
                if self.transport:
                    self.transport.close()
            finally:
                self.upper_layer.receive_data(
                    "Disconnected. Local mode.\r\n".encode("ascii", "ignore")
                )

    def get_info_string(self) -> str:
        """Return a string with information about the SSH connection."""
        return f"SSH V2 - {self.username}@{self.host}: {self.port}"

    def close(self) -> None:
        """Close the SSH connection and stop the backend thread."""
        self._running = False
        if self.channel is not None and not self.channel.closed:
            self.channel.close()
        self.channel = None

        if self.transport is not None and self.transport.is_active():
            self.transport.close()
        self.transport = None

        if self._rx_thread:
            self._rx_thread.join()
