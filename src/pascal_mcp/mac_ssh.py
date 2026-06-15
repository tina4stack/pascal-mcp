"""SSH-to-Mac plumbing for iOS Simulator control and arbitrary remote commands.

PAClient covers file transfer + iOS bundle ops (codesign, IPA, device install)
but does NOT expose arbitrary command execution on the remote — so anything
that needs `xcrun simctl`, `idb`, `xcrun devicectl`, log inspection, or similar
has to go over SSH directly.

This module wraps the OS's openssh client (available on every modern Windows
and macOS / Linux box by default — no third-party Python deps). Auth is key-
based; password storage is intentionally not supported because it's the wrong
direction for any tool that might end up in a CI environment.

Setup the user has to do once, on the Windows machine:

    ssh-copy-id <mac_user>@<mac_host>

(They'll be asked for the Mac password the first time; after that it's keys
all the way.)

The connection profile name is used to derive the host — the user provides
the Mac user separately because PAServer profiles don't store it.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass


def find_ssh() -> str:
    """Locate a native ssh client, preferring Windows OpenSSH.

    We do NOT rely on bare `ssh` + PATH resolution. The MCP server inherits
    whatever environment Claude Desktop was launched with, and in practice
    that PATH can resolve `ssh` to an MSYS2 / Git-bundled binary that hangs
    when invoked from a native Windows process without a pty (it times out
    instead of authenticating). Pinning the native Windows OpenSSH binary
    makes ssh behave identically regardless of who spawned us.

    Order on Windows:
      1. %SystemRoot%\\System32\\OpenSSH\\ssh.exe  (the built-in client)
      2. C:\\Program Files\\OpenSSH-Win64\\ssh.exe  (standalone install)
      3. bare "ssh" as a last resort.
    On non-Windows, just use "ssh".
    """
    if sys.platform == "win32":
        candidates = [
            os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"),
                "System32", "OpenSSH", "ssh.exe",
            ),
            r"C:\Program Files\OpenSSH-Win64\ssh.exe",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
    return "ssh"


@dataclass
class SSHResult:
    """Outcome of a remote command execution."""
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    host: str
    user: str
    command: str

    def summarise(self) -> str:
        lines = [
            f"host:     {self.user}@{self.host}",
            f"command:  {self.command}",
            f"success:  {self.success}",
            f"exit:     {self.exit_code}",
        ]
        if self.stdout:
            lines.append("\n--- stdout ---")
            lines.append(self.stdout.rstrip())
        if self.stderr:
            lines.append("\n--- stderr ---")
            lines.append(self.stderr.rstrip())
        return "\n".join(lines)


def ssh_run(
    host: str,
    user: str,
    command: str,
    key_path: str | None = None,
    timeout: int = 60,
    connect_timeout: int = 10,
    accept_new_host_keys: bool = True,
) -> SSHResult:
    """Run a single command on the remote Mac via SSH.

    Always uses BatchMode (no interactive password prompts) — if the user
    hasn't installed a public key yet, this fails fast with a clear
    "Permission denied (publickey,...)" error rather than hanging.

    Uses a pinned native ssh binary (see find_ssh) instead of bare "ssh"
    so behaviour doesn't depend on whatever PATH the MCP server inherited.

    Args:
        host: Mac hostname or IP (typically the same address as the
            PAServer profile's Host field).
        user: Mac user account. Doesn't have to match the Windows user
            and usually doesn't — PAServer profiles don't store this.
        command: The remote command. Will be wrapped in a shell on the
            Mac side, so quote any internal spaces yourself.
        key_path: Optional path to a specific SSH private key. Defaults
            to whatever the user's ~/.ssh/config + agent provide.
        timeout: Total seconds before the remote command is killed.
        connect_timeout: Seconds before giving up on the initial TCP /
            SSH handshake. Default 10 — a sleeping Mac can take several
            seconds to wake and accept the first connection.
        accept_new_host_keys: Auto-accept new server keys (StrictHostKey
            Checking=accept-new) the first time we see the Mac. Subsequent
            connections still verify the key.

    Returns SSHResult with the full stdout/stderr/exit_code for the
    caller to format and surface.
    """
    args = [find_ssh()]
    if accept_new_host_keys:
        args += ["-o", "StrictHostKeyChecking=accept-new"]
    args += [
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={connect_timeout}",
    ]
    if key_path:
        args += ["-i", key_path]
    args += [f"{user}@{host}", command]

    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return SSHResult(
            success=False, exit_code=-1,
            stdout="",
            stderr=(
                f"ssh command timed out after {timeout}s "
                f"(ssh binary: {find_ssh()})"
            ),
            host=host, user=user, command=command,
        )
    except FileNotFoundError as e:
        return SSHResult(
            success=False, exit_code=-1,
            stdout="",
            stderr=f"ssh binary not found: {e}",
            host=host, user=user, command=command,
        )

    return SSHResult(
        success=proc.returncode == 0,
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        host=host, user=user, command=command,
    )


def ssh_check(
    host: str, user: str, key_path: str | None = None, timeout: int = 15,
) -> tuple[bool, str]:
    """Quick "is SSH reachable + authenticated" probe.

    Runs `whoami` so we can confirm both connectivity AND that we landed
    on the expected account. Returns (ok, message) where message is
    human-readable diagnostic text.

    Default timeout is 15s: a sleeping Mac needs a few seconds to wake on
    the first connection, and 5s was too tight in practice.
    """
    result = ssh_run(
        host, user, "whoami",
        key_path=key_path,
        timeout=timeout, connect_timeout=timeout,
    )
    if result.success and result.stdout.strip() == user:
        return True, f"SSH OK — connected as {user}@{host}"
    if "Permission denied" in result.stderr:
        return False, (
            f"SSH key auth failed for {user}@{host}. "
            "Install your public key on the Mac (one-time setup):\n"
            f"  ssh-copy-id {user}@{host}\n"
            "You'll be prompted for the Mac password once. After that, the "
            "MCP tools authenticate via key automatically."
        )
    if "Connection refused" in result.stderr:
        return False, (
            f"SSH port refused on {host}. Enable Remote Login on the Mac: "
            "System Settings → General → Sharing → Remote Login."
        )
    if "timed out" in result.stderr.lower():
        return False, (
            f"SSH to {host} timed out. Check host reachability and firewall."
        )
    return False, f"SSH check failed: {result.stderr or '(no error text)'}"


def shell_quote(s: str) -> str:
    """Safely quote a string for inclusion in a remote shell command."""
    return shlex.quote(s)
