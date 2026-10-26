"""Runs the slot-switch helper on lab PCs over Tailscale SSH.

The slot agent holds no SSH key. Its sidecar node (tag:scheduler) is allowed by the policy's `ssh`
rules to log in to every lab PC as `labsched` only, and the PC's tailscaled checks that against the
tailnet identity, so plain `ssh` with no credentials is enough. In the container the tailnet is a
userspace Tailscale sidecar exposing SOCKS5 on 127.0.0.1:1055, so ssh goes through `nc -X 5`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

HELPER = "/usr/local/sbin/slot-switch"


@dataclass
class SSHResult:
    ok: bool
    output: str


class SSH:
    def __init__(self, known_hosts: str, user: str = "labsched", socks: str = "", timeout: int = 60):
        self.known_hosts = known_hosts
        self.user = user
        self.socks = socks
        self.timeout = timeout

    def switch(self, host: str, account: str | None) -> SSHResult:
        """Make `account` the only slot-active account on host ('-' = nobody)."""
        Path(self.known_hosts).parent.mkdir(parents=True, exist_ok=True)
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=no",
               "-o", "KbdInteractiveAuthentication=no", "-o", "StrictHostKeyChecking=accept-new",
               "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "ConnectTimeout=15"]
        if self.socks:
            cmd += ["-o", f"ProxyCommand=nc -X 5 -x {self.socks} %h %p"]
        cmd += [f"{self.user}@{host}", "sudo", HELPER, account or "-"]
        try:
            p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return SSHResult(False, "timed out")
        out = (p.stdout + p.stderr).strip()
        return SSHResult(p.returncode == 0, out[-2000:])
