#!/usr/bin/env python3
"""
SSH V2 backend for ASR-33 emulator.

Cross-platform, OpenSSH/PowerShell-like behavior:
- Uses known_hosts (user + system where available)
- Supports TOFU (trust on first use) with auto-add
- Optional explicit host key fingerprint pinning
"""

import base64
import hashlib
import os
import queue
import socket
import threading
import time
from typing import Any, List, Tuple

import paramiko
from paramiko.agent import Agent
from paramiko.ssh_exception import (
    SSHException,
    PasswordRequiredException,
    AuthenticationException,
)

class HostKeyVerificationError(SSHException):
    """Raised when the SSH server's host key does not match expectations."""
    pass  # pylint: disable=unnecessary-pass


class SSHV2Backend:
    """SSH V2 backend for ASR-33 emulator."""

    def __init__(self, upper_layer: Any, config):
        self.upper_layer = upper_layer

        # Basic connection parameters
        self.host = config.get("host", default="localhost")
        self.username = config.get("username", default="user")
        self.port = config.get("port", default=22)

        # Security / host-key configuration
        self.expected_fingerprint = config.get("expected_fingerprint", default=None)
        # "strict", "accept-new", or "off" (like StrictHostKeyChecking yes/accept-new/no)
        self.host_key_policy = config.get("host_key_policy", default="accept-new")
        # Optional override for known_hosts file
        self.known_hosts_file = config.get("known_hosts_file", default=None)

        # Authentication
        self.key_filename = config.get("key_filename", default=None)
        self.password = config.get("password", default=None)  # optional

        self._running = True
        self._input_queue: "queue.Queue[str]" = queue.Queue(maxsize=2048)

        self.transport: paramiko.Transport | None = None
        self.channel = None
        self._rx_thread: threading.Thread | None = None

        # Buffer and flag for password input
        self._buffer = ""
        self._waiting_for_password = False

    # ---------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------

    def start(self):
        """Start the SSH thread once the frontend is ready."""
        self._rx_thread = threading.Thread(target=self.ssh_thread, daemon=True)
        self._rx_thread.start()

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

    def get_info_string(self) -> str:
        """Return a string with information about the SSH connection."""
        return f"SSH V2 - {self.username}@{self.host}: {self.port}"

    # ---------------------------------------------------------------------
    # I/O with upper layer
    # ---------------------------------------------------------------------

    def send_data(self, data: bytes) -> None:
        """Send data received from upper layer to the SSH channel."""
        # Accept str, memoryview, bytearray and convert to bytes
        if isinstance(data, str):
            data = data.encode("ascii", "ignore")
        elif isinstance(data, memoryview):
            data = data.tobytes()
        elif isinstance(data, bytearray):
            data = bytes(data)

        if self.channel is not None:
            # Normal case: we already have a live SSH channel
            self.channel.send(data)
            return

        # No channel yet: treat input as interactive (for password, etc.)
        masked_out = None
        try:
            ch = data.decode("ascii", "ignore")
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
                    if self._buffer:
                        self._buffer = self._buffer[:-1]
                else:
                    self._buffer += c
        except (AttributeError, TypeError, UnicodeDecodeError):
            masked_out = None

        # Echo rules
        if self._waiting_for_password:
            if masked_out:
                self.upper_layer.receive_data(masked_out)
        else:
            self.upper_layer.receive_data(data)

    def keyboard_interactive_handler(self, title, instructions, prompt_list):
        """Respond to keyboard-interactive prompts from the SSH server."""
        responses: List[str] = []
        if title:
            self.upper_layer.receive_data((title + "\r\n").encode("ascii", "ignore"))
        if instructions:
            self.upper_layer.receive_data(
                (instructions + "\r\n").encode("ascii", "ignore")
            )

        for prompt, _ in prompt_list:
            self.upper_layer.receive_data((prompt + "\r\n").encode("ascii", "ignore"))

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

    # ---------------------------------------------------------------------
    # Known_hosts handling (OpenSSH-like)
    # ---------------------------------------------------------------------

    def _known_hosts_paths(self) -> List[str]:
        """
        Return a list of candidate known_hosts paths, cross-platform.

        - User file (~/.ssh/known_hosts) always first (OpenSSH style).
        - System files where they exist (/etc/ssh/ssh_known_hosts, etc.).
        """
        paths: List[str] = []

        # User known_hosts
        if self.known_hosts_file:
            paths.append(os.path.expanduser(self.known_hosts_file))
        else:
            home = os.path.expanduser("~")
            ssh_dir = os.path.join(home, ".ssh")
            paths.append(os.path.join(ssh_dir, "known_hosts"))

        # System-wide (mostly non-Windows, but harmless if missing)
        paths.extend(
            [
                "/etc/ssh/ssh_known_hosts",
                "/etc/ssh/ssh_known_hosts2",
            ]
        )

        # Deduplicate while preserving order
        seen = set()
        deduped = []
        for p in paths:
            if p not in seen:
                seen.add(p)
                deduped.append(p)
        return deduped

    def load_known_hosts(self) -> Tuple[paramiko.HostKeys, str | None]:
        """Load known hosts from standard locations. Returns (hostkeys, primary_user_file)."""
        hostkeys = paramiko.HostKeys()
        primary_user_file = None

        paths = self._known_hosts_paths()
        for i, path in enumerate(paths):
            try:
                hostkeys.load(path)
                # First path is considered primary user file
                if i == 0:
                    primary_user_file = path
            except IOError:
                # Ignore missing/unreadable files, OpenSSH-style
                continue

        # Even if not loaded, first path is where we'll save new entries
        if primary_user_file is None and paths:
            primary_user_file = paths[0]

        return hostkeys, primary_user_file

    def save_known_host(self, host: str, port: int, key, known_hosts_path: str) -> None:
        """
        Append host key to known_hosts (OpenSSH-style TOFU).

        - host key line format: [host]:port type base64
        """
        host_for_file = host if port == 22 else f"[{host}]:{port}"

        # Ensure directory exists
        directory = os.path.dirname(os.path.expanduser(known_hosts_path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)

        line = f"{host_for_file} {key.get_name()} {key.get_base64()}\n"
        with open(os.path.expanduser(known_hosts_path), "a", encoding="utf-8") as f:
            f.write(line)

    def verify_host_key_known_hosts(self, server_key) -> None:
        """
        OpenSSH/PowerShell-like host key behavior.

        Modes:
        - host_key_policy == "strict":
            - If host is in known_hosts: key must match or fail (changed host key).
            - If host is NOT in known_hosts: fail (unknown host).
        - host_key_policy == "accept-new" (default, like TOFU):
            - If host is in known_hosts: key must match or fail.
            - If host is NOT in known_hosts: accept and auto-add to user known_hosts.
        - host_key_policy == "off":
            - Do nothing (no known_hosts checking or writing).
        """
        policy = (self.host_key_policy or "accept-new").lower()
        if policy not in {"strict", "accept-new", "off"}:
            policy = "accept-new"

        if policy == "off":
            # Completely bypass known_hosts handling
            return

        hostkeys, primary_user_file = self.load_known_hosts()
        host_patterns = [self.host, f"[{self.host}]:{self.port}"]

        found = False
        matched = False

        for pattern in host_patterns:
            if pattern in hostkeys:
                found = True
                for _, known_key in hostkeys[pattern].items():
                    if known_key == server_key:
                        matched = True
                        break
                if matched:
                    break

        if found and not matched:
            # Known host, key mismatch => OpenSSH-style "REMOTE HOST IDENTIFICATION HAS CHANGED"
            raise HostKeyVerificationError(
                f"REMOTE HOST IDENTIFICATION HAS CHANGED for {self.host}"
            )

        if found and matched:
            # Everything is fine
            return

        # Not found in known_hosts
        # Not found in known_hosts
        if policy == "strict":
            raise HostKeyVerificationError(
                f"Unknown host {self.host} (not in known_hosts; strict mode)"
            )

        if policy == "accept-new":
            # Ask user: yes/no/once
            decision = self.prompt_tofu_decision(server_key)

            if decision == "yes":
                # Permanently add to known_hosts
                if primary_user_file:
                    try:
                        self.save_known_host(self.host, self.port, server_key, primary_user_file)
                        self.upper_layer.receive_data(
                            f"Warning: Permanently added '{self.host}' "
                            f"to known_hosts.\r\n".encode("ascii", "ignore")
                        )
                    except OSError:
                        # If we can't write, still allow connection
                        self.upper_layer.receive_data(
                            "Warning: Could not write to known_hosts.\r\n".encode("ascii", "ignore")
                        )
                return

            if decision == "once":
                # Trust only for this session
                return

    # ---------------------------------------------------------------------
    # Explicit fingerprint pinning
    # ---------------------------------------------------------------------

    @staticmethod
    def _format_sha256_fingerprint(server_key) -> str:
        raw_key = server_key.asbytes()
        return "SHA256:" + base64.b64encode(
            hashlib.sha256(raw_key).digest()
        ).decode().rstrip("=")

    def verify_explicit_fingerprint(self, server_key) -> None:
        """Verify the server's host key against an explicit expected fingerprint."""
        if not self.expected_fingerprint:
            return

        sha256_fp = self._format_sha256_fingerprint(server_key)

        if sha256_fp != self.expected_fingerprint:
            raise HostKeyVerificationError(
                "Host key verification failed!\r\n"
                f"Expected: {self.expected_fingerprint}\r\n"
                f"Received: {sha256_fp}\r\n"
            )

    # ---------------------------------------------------------------------
    # Cross-platform key loading (agent + files, OpenSSH-style)
    # ---------------------------------------------------------------------

    def _iter_agent_keys(self):
        """
        Yield keys from the SSH agent (ssh-agent on *nix, Pageant on Windows)
        if available. This mirrors how OpenSSH will first try agent keys.
        """
        try:
            agent = Agent()
            for key in agent.get_keys():
                yield key
        except SSHException:
            # No agent, or agent not reachable
            return

    def _iter_default_key_paths(self):
        """
        Yield candidate private key paths in OpenSSH style:
        - ~/.ssh/id_ed25519
        - ~/.ssh/id_ecdsa
        - ~/.ssh/id_rsa
        (On Windows this still resolves correctly via os.path.expanduser.)
        """
        home = os.path.expanduser("~")
        ssh_dir = os.path.join(home, ".ssh")

        candidates = [
            os.path.join(ssh_dir, "id_ed25519"),
            os.path.join(ssh_dir, "id_ecdsa"),
            os.path.join(ssh_dir, "id_rsa"),
        ]

        for path in candidates:
            if os.path.exists(path):
                yield path

    def _load_key_from_path(self, path: str):
        """
        Load a private key from the given path, trying common key types.
        Returns:
            paramiko.PKey instance or None if loading failed.
        """
        path = os.path.expanduser(path)

        loaders = [
            paramiko.Ed25519Key.from_private_key_file,
            paramiko.ECDSAKey.from_private_key_file,
            paramiko.RSAKey.from_private_key_file,
        ]

        for loader in loaders:
            try:
                return loader(path)
            except PasswordRequiredException:
                # Encrypted key; your backend doesn't support passphrases yet
                return None
            except (SSHException, OSError, IOError):
                continue

        return None

    def load_all_candidate_keys(self):
        """
        Cross-platform, OpenSSH-like key discovery.

        Search order:
        1. Key explicitly configured via self.key_filename
        2. Keys from SSH agent (ssh-agent or Pageant)
        3. Default private keys in ~/.ssh (id_ed25519, id_ecdsa, id_rsa)

        Returns:
            List of (pkey, description) tuples.
        """
        keys = []

        # 1. Explicit key from config
        if self.key_filename:
            pkey = self._load_key_from_path(self.key_filename)
            if pkey is not None:
                keys.append((pkey, f"config key ({self.key_filename})"))

        # 2. Agent keys (if any)
        for agent_key in self._iter_agent_keys():
            keys.append((agent_key, "agent key"))

        # 3. Default keys from ~/.ssh
        for path in self._iter_default_key_paths():
            pkey = self._load_key_from_path(path)
            if pkey is not None:
                keys.append((pkey, f"default key ({path})"))

        return keys

    def prompt_tofu_decision(self, server_key):
        """
        Ask the user whether to trust an unknown host key:
        yes  -> trust permanently (write to known_hosts)
        no   -> abort connection
        once -> trust only for this session
        """
        fp = self._format_sha256_fingerprint(server_key)

        warning = (
            f"The authenticity of host '{self.host}' cannot be established.\r\n"
            f"Key type: {server_key.get_name()}\r\n"
            f"Fingerprint: {fp}\r\n"
            f"Are you sure you want to continue connecting (yes/no/once)? "
        )
        self.upper_layer.receive_data(warning.encode("ascii", "ignore"))

        # Wait for user input
        response = ""
        while self._running:
            try:
                response = self._input_queue.get(timeout=0.1).strip().lower()
                break
            except queue.Empty:
                continue

        self.upper_layer.receive_data(b"\r\n")

        if response == "yes":
            return "yes"
        if response == "once":
            return "once"
        if response == "no":
            raise HostKeyVerificationError("User rejected unknown host key")

        # Invalid response → behave like OpenSSH: treat as "no"
        raise HostKeyVerificationError("User rejected unknown host key")

    # ---------------------------------------------------------------------
    # SSH thread
    # ---------------------------------------------------------------------
    def ssh_thread(self) -> None:
        """Background thread: manage SSH connection and data transfer."""
        time.sleep(1)  # brief delay to allow frontend to initialize
        try:
            sock = socket.create_connection((self.host, self.port), timeout=10)
            self.transport = paramiko.Transport(sock)
            self.transport.start_client(timeout=10)

            server_key = self.transport.get_remote_server_key()

            # 1. Explicit fingerprint pinning (if configured)
            self.verify_explicit_fingerprint(server_key)

            # 2. OpenSSH-style known_hosts handling (TOFU / strict / off)
            self.verify_host_key_known_hosts(server_key)

            # ---------------- Authentication sequence ----------------

            # 1. Try all candidate keys (config, agent, defaults)
            if not self.transport.is_authenticated():
                for pkey, desc in self.load_all_candidate_keys():
                    try:
                        self.transport.auth_publickey(self.username, pkey)
                        if self.transport.is_authenticated():
                            break
                    except (AuthenticationException, SSHException) as e:
                        # Optional: emit a small debug message to the terminal
                        self.upper_layer.receive_data(
                            f"Public key auth failed with {desc}: {e}\r\n".encode("ascii", "ignore")
                        )
                        continue

            # 2. Try default keys (~/.ssh/id_ed25519, ~/.ssh/id_rsa) if not yet authenticated
            if not self.transport.is_authenticated():
                home = os.path.expanduser("~")
                ssh_dir = os.path.join(home, ".ssh")
                candidate_keys = [
                    os.path.join(ssh_dir, "id_ed25519"),
                    os.path.join(ssh_dir, "id_rsa"),
                ]
                for key_path in candidate_keys:
                    if not os.path.exists(key_path):
                        continue
                    try:
                        pkey = None
                        # Try Ed25519 first if available
                        if key_path.endswith("id_ed25519"):
                            try:
                                pkey = paramiko.Ed25519Key.from_private_key_file(key_path)
                            except (PasswordRequiredException, SSHException, OSError):
                                pkey = None

                        # Fall back to RSA
                        if pkey is None:
                            pkey = paramiko.RSAKey.from_private_key_file(key_path)

                        self.transport.auth_publickey(self.username, pkey)
                        if self.transport.is_authenticated():
                            break
                    except PasswordRequiredException:
                        continue
                    except (SSHException, OSError, IOError):
                        continue

            # 3. Password authentication (with optional prompt)
            if not self.transport.is_authenticated():
                if not self.password:
                    prompt = (
                        f"Password for {self.username}@{self.host} on port {self.port}: "
                    )
                    self.upper_layer.receive_data(prompt.encode("ascii", "ignore"))
                    self._waiting_for_password = True
                    try:
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
                    self.upper_layer.receive_data(b"\r\n")

                try:
                    if self.password is None:
                        raise SSHException("No password provided for auth_password")
                    self.transport.auth_password(self.username, self.password)
                except (AuthenticationException, SSHException) as e:
                    self.upper_layer.receive_data(
                        f"Password authentication failed: {e}\r\n".encode(
                            "ascii", "ignore"
                        )
                    )
                finally:
                    # Clear password after use
                    self.password = None

            # 4. Keyboard-interactive (for OTP/2FA)
            if not self.transport.is_authenticated():
                try:
                    self.transport.auth_interactive(
                        self.username, self.keyboard_interactive_handler
                    )
                except (SSHException, AuthenticationException, OSError) as e:
                    self.upper_layer.receive_data(
                        f"Keyboard-interactive failed: {e}\r\n".encode(
                            "ascii", "ignore"
                        )
                    )

            if not self.transport.is_authenticated():
                raise SSHException("Authentication failed")

            # ---------------- Shell setup ----------------
            self.channel = self.transport.open_session()
            self.channel.get_pty(term="tty33")
            self.channel.invoke_shell()

            while self._running and self.channel is not None and not self.channel.closed:
                if self.channel.recv_ready():
                    data = self.channel.recv(4096)
                    if data:
                        self.upper_layer.receive_data(data)
                else:
                    time.sleep(0.01)

        except socket.gaierror:
            self.upper_layer.receive_data(
                f"Network error: Unable to resolve or reach {self.host}. "
                f"Is the device offline?\r\n".encode("ascii", "ignore")
            )
        except socket.timeout:
            self.upper_layer.receive_data(
                f"Connection to {self.host}:{self.port} timed out.\r\n".encode(
                    "ascii", "ignore"
                )
            )
        except ConnectionRefusedError:
            self.upper_layer.receive_data(
                f"Connection refused by {self.host}:{self.port}."
                f" Is SSH running?\r\n".encode("ascii", "ignore")
            )
        except HostKeyVerificationError as e:
            self.upper_layer.receive_data(
                f"SECURITY ERROR: {e}\r\n".encode("ascii", "ignore")
            )
        except SSHException as e:
            self.upper_layer.receive_data(
                f"SSH error: {e}\r\n".encode("ascii", "ignore")
            )
        except (OSError, RuntimeError, ValueError) as e:
            self.upper_layer.receive_data(
                f"Unexpected error: {e}\r\n".encode("ascii", "ignore")
            )
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
