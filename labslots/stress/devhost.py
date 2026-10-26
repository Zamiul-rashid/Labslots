#!/usr/bin/env python3
"""Simulated student devices on one machine, as plain tailscaled processes: userspace networking, no root,
no Docker, nothing installed. scale.py copies this file, the Tailscale binaries and the test CA to
~/lsx-stress on each device machine and runs it over SSH. Every command prints JSON.

    devhost.py start           stdin [{"name", "key", "login"}]: start each device and sign it in
    devhost.py stop NAME...    stop devices (their state stays, like a laptop that is shut)
    devhost.py restart NAME... stop and start again from the saved state (a reboot: no new sign-in)
    devhost.py list            {name: {"alive", "rss_mb"}}
    devhost.py probe           stdin [{"name", "ip", "user", "timeout"?}]: a Tailscale SSH login from that device
    devhost.py stats           free memory, load, and the devices' total memory and CPU time
    devhost.py teardown        stop every device and delete ~/lsx-stress

Devices run at nice 19, so the machine's own services always come first.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOME = Path(__file__).resolve().parent
BIN, DEV, CA = HOME / "bin", HOME / "dev", HOME / "ca.crt"


def env() -> dict:
    # GOMAXPROCS/GOGC keep each simulated device small; they change nothing Headscale sees.
    return dict(os.environ, SSL_CERT_FILE=str(CA), GOMAXPROCS="1", GOGC="50", TS_NO_LOGS_NO_SUPPORT="true")


def pid_of(name: str) -> int | None:
    try:
        pid = int((DEV / name / "pid").read_text())
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    return pid if str(BIN / "tailscaled").encode() in cmd else None


def rss_mb(pid: int) -> float:
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def private_file(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def launch(d: dict, sign_in: bool) -> None:
    DEV.mkdir(mode=0o700, parents=True, exist_ok=True)
    dd = DEV / d["name"]
    dd.mkdir(mode=0o700, exist_ok=True)
    # No key here: a restart reuses tailscaled's saved state and never signs in again.
    private_file(dd / "login.json", json.dumps({"name": d["name"], "login": d["login"]}))
    if pid_of(d["name"]):
        return
    sock = dd / "sock"
    sock.unlink(missing_ok=True)
    log = open(dd / "log", "ab")
    p = subprocess.Popen(["nice", "-n", "19", str(BIN / "tailscaled"), "--tun=userspace-networking",
                          f"--statedir={dd}", f"--socket={sock}", "--port=0", "--no-logs-no-support"],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=env(), start_new_session=True)
    (dd / "pid").write_text(str(p.pid))
    if sign_in:
        for _ in range(100):
            if sock.exists():
                break
            time.sleep(0.1)
        private_file(dd / "authkey", d["key"])  # read by tailscale up; never on a command line (ps)
        subprocess.Popen(["nice", "-n", "19", str(BIN / "tailscale"), f"--socket={sock}", "up",
                          f"--login-server={d['login']}", f"--auth-key=file:{dd / 'authkey'}", f"--hostname={d['name']}",
                          "--accept-dns=false", "--timeout=300s"],
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL, env=env(), start_new_session=True)


def stop(names: list[str]) -> dict:
    done = []
    for n in names:
        pid = pid_of(n)
        if pid:
            os.kill(pid, signal.SIGTERM)
            done.append(n)
    return {"stopped": done}


def probe(jobs: list[dict]) -> list[dict]:
    def one(j: dict) -> dict:
        sock = DEV / j["name"] / "sock"
        cmd = ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "PubkeyAuthentication=no",
               "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no", "-o", "IdentitiesOnly=yes",
               "-o", f"ProxyCommand={BIN / 'tailscale'} --socket={sock} nc %h %p", f"{j['user']}@{j['ip']}", "true"]
        t0 = time.monotonic()
        # Own process group: on a timeout the ProxyCommand (tailscale nc) is killed with ssh, not orphaned.
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             stdin=subprocess.DEVNULL, env=env(), start_new_session=True)
        try:
            out, _ = p.communicate(timeout=j.get("timeout", 15))
            ok, out = p.returncode == 0, out.strip()
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            ok, out = False, "timed out"
        return {"ok": ok, "ms": round((time.monotonic() - t0) * 1000), "out": out[-160:]}
    with ThreadPoolExecutor(12) as pool:
        return list(pool.map(one, jobs))


def stats() -> dict:
    mem = {l.split(":")[0]: int(l.split()[1]) // 1024 for l in open("/proc/meminfo")}
    n = rss = ticks = 0
    for d in DEV.iterdir() if DEV.exists() else []:
        pid = pid_of(d.name)
        if not pid:
            continue
        n += 1
        rss += rss_mb(pid)
        try:
            f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            ticks += int(f[11]) + int(f[12])
        except (OSError, IndexError, ValueError):
            pass
    return {"devices": n, "rss_mb": round(rss, 1), "cpu_s": round(ticks / os.sysconf("SC_CLK_TCK"), 1),
            "mem_available_mb": mem.get("MemAvailable", 0), "load1": float(open("/proc/loadavg").read().split()[0]),
            "cores": os.cpu_count()}


def main(args: list[str]) -> object:
    cmd = args[0]
    if cmd == "start":
        devs = json.load(sys.stdin)
        for d in devs:
            launch(d, sign_in=True)
        return {"started": len(devs)}
    if cmd == "stop":
        return stop(args[1:])
    if cmd == "restart":
        stop(args[1:])
        time.sleep(1)
        for n in args[1:]:
            launch(json.loads((DEV / n / "login.json").read_text()), sign_in=False)
        return {"restarted": args[1:]}
    if cmd == "list":
        out = {}
        for d in sorted(DEV.iterdir()) if DEV.exists() else []:
            pid = pid_of(d.name)
            out[d.name] = {"alive": bool(pid), "rss_mb": round(rss_mb(pid), 1) if pid else 0}
        return out
    if cmd == "probe":
        return probe(json.load(sys.stdin))
    if cmd == "stats":
        return stats()
    if cmd == "teardown":
        names = [d.name for d in DEV.iterdir()] if DEV.exists() else []
        stop(names)
        time.sleep(2)
        for n in names:
            pid = pid_of(n)
            if pid:
                os.kill(pid, signal.SIGKILL)
        shutil.rmtree(HOME)
        return {"stopped": len(names), "removed": str(HOME)}
    raise SystemExit(__doc__)


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1:])))
