#!/usr/bin/env python3
"""Stress test of the labslots system on its own Headscale: 70 lab PCs, 57 groups, 80 students.

    python3 run.py seed        (called by setup.sh) fill the hub's database with the schedule
    python3 run.py             run every phase and write CSVs to results/<run>/

Phases: PCs join -> students join -> steady -> slot change 1 (clean-up off) -> churn ->
slot change 2 (clean-up on: outgoing students removed) -> slot change 3 (incoming students sign in) ->
second device per student -> slot change 4.  Each "slot change" rewrites the current slot's bookings in the
hub, exactly what a new slot (or a Sheet edit) does, and times how the slot agent carries it out.

CSV files (all times are Asia/Dhaka, like the app):
  resources.csv   time, phase, container, count, cpu_pct, mem_mb
  probes.csv      time, phase, t_change_s, student, group, pc, expected, result, ms
  events.csv      time, phase, t_phase_s, t_change_s, event, value, detail
  agent_runs.csv  time, phase, run_s, ssh_s, policy_changed, removed, would_remove, switched, errors, first_error
  pages.csv       time, phase, page, status, ms
  api.csv         time, phase, call, ms, nodes, online
  summary.csv     phase, metric, value, unit
"""

from __future__ import annotations

import csv
import http.cookiejar
import json
import random
import sqlite3
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DHAKA = ZoneInfo("Asia/Dhaka")
HUB_DB = HERE / "hub-data" / "labslots.db"
N_PCS, N_GROUPS, N_USERS = 70, 57, 80
LOGIN = "https://hs-stress.test:8443"
TS_IMAGE = "tailscale/tailscale:v1.102.3"
PALETTE = ("#ffe08a", "#b8e6b0", "#f7c6d9", "#bcdcf5", "#ffd0a6", "#d9ccf2", "#c9e7e1", "#f4d6b8",
           "#e9f0a6", "#a8dcd1", "#f2b8b0", "#c6d4f7", "#f7e3b0", "#d6eac3", "#e8c9ef", "#b9e3ef")
rnd = random.Random(42)


def now() -> datetime:
    return datetime.now(DHAKA).replace(tzinfo=None)


def iso(t: datetime | None = None) -> str:
    return (t or now()).isoformat(timespec="seconds")


def sh(args: list[str], timeout: float = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def hs(*args: str):
    rc, out = sh(["docker", "exec", "stress-hs", "headscale", *args, "-o", "json"])
    if rc:
        raise RuntimeError(f"headscale {' '.join(args)}: {out}")
    return json.loads(out) if out else None


def ct_ip() -> str:
    return sh(["sh", "-c", "ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1"])[1]


def slots_now() -> list[tuple[int, str]]:
    """The slot running now and the next one (both get the same assignment, in case a boundary passes)."""
    d = now()
    if 6 <= d.hour < 18:
        cur = (d.weekday(), "day")
    elif d.hour >= 18:
        cur = (d.weekday(), "night")
    else:
        cur = ((d - timedelta(days=1)).weekday(), "night")
    nxt = (cur[0], "night") if cur[1] == "day" else ((cur[0] + 1) % 7, "day")
    return [cur, nxt]


def group_of(user: int) -> int:
    return (user - 1) % N_GROUPS + 1


# ----------------------------------------------------------------------------------------------- seed
def seed() -> None:
    db = sqlite3.connect(HUB_DB)
    today = date.today()
    db.execute("DELETE FROM bookings"); db.execute("DELETE FROM members"); db.execute("DELETE FROM groups")
    db.execute("DELETE FROM pcs"); db.execute("DELETE FROM terms")
    db.execute("INSERT INTO terms(id, name, start_date, end_date) VALUES (1, 'Stress term', ?, ?)",
               ((today - timedelta(days=30)).isoformat(), (today + timedelta(days=150)).isoformat()))
    for k in range(1, N_PCS + 1):
        db.execute("INSERT INTO pcs(id, name, tag, ssh_enabled) VALUES (?, ?, ?, 1)", (k, f"pc-{k}", f"tag:pc-{k}"))
    for g in range(1, N_GROUPS + 1):
        db.execute("INSERT INTO groups(id, name, slug, linux_account, color, short) VALUES (?, ?, ?, ?, ?, ?)",
                   (g, f"Group {g:02d}", f"g{g:02d}", f"grp-g{g:02d}", PALETTE[(g - 1) % len(PALETTE)], f"G{g:02d}"))
    for u in range(1, N_USERS + 1):
        db.execute("INSERT INTO members(group_id, email) VALUES (?, ?)", (group_of(u), f"s{u:02d}@stress.test"))
    for k in range(1, N_PCS + 1):           # a full week so the calendar pages have realistic data
        for wd in range(7):
            for h, half in enumerate(("day", "night")):
                if wd == 6 or rnd.random() < 0.07:
                    continue
                g = (k * 3 + wd * 7 + h * 29) % N_GROUPS + 1
                db.execute("INSERT INTO bookings(term_id, pc_id, group_id, weekday, half, status, requested_by, "
                           "requested_at) VALUES (1, ?, ?, ?, ?, 'approved', 'stress', '')", (k, g, wd, half))
    for k, v in (("ssh_enforce", "1"), ("device_cleanup", "0"), ("enforce_policy", "1")):
        db.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (k, v))
    db.commit()
    set_slot(ASSIGN["A"])
    print("seeded: 70 PCs, 57 groups, 80 students, a full week of bookings")


def set_slot(assign: dict[int, int]) -> None:
    db = sqlite3.connect(HUB_DB, timeout=30)
    for wd, half in slots_now():
        db.execute("DELETE FROM bookings WHERE term_id = 1 AND weekday = ? AND half = ?", (wd, half))
        db.executemany("INSERT INTO bookings(term_id, pc_id, group_id, weekday, half, status, requested_by, requested_at) "
                       "VALUES (1, ?, ?, ?, ?, 'approved', 'stress', '')", [(k, g, wd, half) for k, g in assign.items()])
    db.commit()
    db.close()


def setting(key: str, value: str) -> None:
    db = sqlite3.connect(HUB_DB, timeout=30)
    db.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               (key, value))
    db.commit()
    db.close()


# Assignments for the slot running now. A: 65 PCs busy, every group active. B: same load, rotated.
# C: 30 groups on two PCs each (groups 31-57 out). D: groups 28-57 in (1-27 out, 31-57 must sign in again).
ASSIGN = {
    "A": {k: (k - 1) % N_GROUPS + 1 for k in range(1, 66)},
    "B": {k: (k - 1 + 19) % N_GROUPS + 1 for k in range(1, 66)},
    "C": {k: (k - 1) // 2 + 1 for k in range(1, 61)},
    "D": {k: (k - 1) // 2 + 28 for k in range(1, 61)},
    "E": {k: (k - 1 + 7) % N_GROUPS + 1 for k in range(1, 66)},
}


# ------------------------------------------------------------------------------------------ the runner
class Run:
    def __init__(self) -> None:
        self.dir = HERE / "results" / now().strftime("%Y%m%d-%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.files, self.writers = {}, {}
        for name, cols in (
                ("resources", ["time", "phase", "container", "count", "cpu_pct", "mem_mb"]),
                ("probes", ["time", "phase", "t_change_s", "student", "group", "pc", "expected", "result", "ms"]),
                ("events", ["time", "phase", "t_phase_s", "t_change_s", "event", "value", "detail"]),
                ("agent_runs", ["time", "phase", "run_s", "ssh_s", "policy_changed", "removed", "would_remove",
                                "switched", "errors", "first_error"]),
                ("pages", ["time", "phase", "page", "status", "ms"]),
                ("api", ["time", "phase", "call", "ms", "nodes", "online"])):
            f = open(self.dir / f"{name}.csv", "w", newline="")
            w = csv.writer(f)
            w.writerow(cols)
            self.files[name], self.writers[name] = f, w
        self.summary: list[list] = []
        self.phase, self.t_phase, self.t_change = "setup", time.monotonic(), None
        self.assign: dict[int, int] = ASSIGN["A"]
        self.cleanup = False
        self.stop = threading.Event()
        self.ip = ct_ip()
        self.keys: dict[str, str] = {}
        self.uids: dict[int, str] = {}
        self.relogin_due: dict[str, float] = {}
        self.started: dict[str, float] = {}
        self.ssl = ssl.create_default_context(cafile=str(HERE / "certs" / "ca.crt"))
        self.api_key = (HERE / "secrets" / "agent" / "headscale_api_key").read_text().strip()
        self.last_report_at = ""
        self.reports: list[dict] = []
        self.probe_rounds: list[dict] = []

    # -- recording ------------------------------------------------------------------------------------
    def row(self, name: str, values: list) -> None:
        with self.lock:
            self.writers[name].writerow(values)
            self.files[name].flush()

    def t_since_change(self) -> str:
        return f"{time.monotonic() - self.t_change:.1f}" if self.t_change else ""

    def event(self, name: str, value="", detail: str = "") -> None:
        t_phase = f"{time.monotonic() - self.t_phase:.1f}"
        self.row("events", [iso(), self.phase, t_phase, self.t_since_change(), name, value, detail])
        print(f"[{iso()[11:]}] {self.phase:<22} {name} {value} {detail}", flush=True)

    def start_phase(self, name: str) -> None:
        self.phase, self.t_phase = name, time.monotonic()
        self.event("phase started")

    def add_summary(self, metric: str, value, unit: str = "", phase: str | None = None) -> None:
        self.summary.append([phase or self.phase, metric, value, unit])

    # -- Headscale API --------------------------------------------------------------------------------
    def nodes(self) -> list[dict]:
        t0 = time.monotonic()
        req = urllib.request.Request(f"{LOGIN}/api/v1/node", headers={"Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, context=self.ssl, timeout=30) as r:
            nodes = json.load(r).get("nodes", [])
        ms = round((time.monotonic() - t0) * 1000)
        self.row("api", [iso(), self.phase, "list nodes", ms, len(nodes), sum(1 for n in nodes if n.get("online"))])
        return nodes

    @staticmethod
    def tags(n: dict) -> list[str]:
        return list(n.get("tags") or [])

    @staticmethod
    def host(n: dict) -> str:
        return n.get("givenName") or n.get("given_name") or n.get("name") or ""

    def pc_ips(self, nodes: list[dict]) -> dict[int, str]:
        out = {}
        for n in nodes:
            ips = [i for i in (n.get("ipAddresses") or n.get("ip_addresses") or []) if "." in i]
            for t in self.tags(n):
                if t.startswith("tag:pc-") and ips:
                    out[int(t[7:])] = ips[0]
        return out

    # -- containers -----------------------------------------------------------------------------------
    def pc_key(self, k: int) -> str:
        return hs("preauthkeys", "create", "--tags", f"tag:pc-{k}", "--reusable", "-e", "24h")["key"]

    def user_key(self, u: int) -> str:
        if u not in self.uids:
            name = f"s{u:02d}@stress.test"
            existing = {x["name"]: x["id"] for x in hs("users", "list") or []}
            self.uids[u] = str(existing.get(name) or hs("users", "create", name)["id"])
        return hs("preauthkeys", "create", "--user", self.uids[u], "--reusable", "-e", "24h")["key"]

    def run_pc(self, k: int) -> None:
        key = self.keys.setdefault(f"pc-{k}", self.pc_key(k))
        sh(["docker", "rm", "-f", f"pc-{k}"])
        rc, out = sh(["docker", "run", "-d", "--name", f"pc-{k}", "--hostname", f"pc-{k}", "--memory", "96m",
                      "--add-host", f"hs-stress.test:{self.ip}", "-e", f"TS_LOGIN={LOGIN}", "-e", f"TS_AUTHKEY={key}",
                      "-e", f"TS_HOSTNAME=pc-{k}", "-e", f"TS_TAGS=tag:pc-{k}", "-e", "SSL_CERT_FILE=/certs/ca.crt",
                      "-v", f"{HERE}/certs:/certs:ro", "stress-pc"])
        if rc:
            self.event("pc start failed", k, out[-200:])

    def run_user(self, u: int, dev: str = "a") -> None:
        name = f"s{u:02d}-{dev}"
        key = self.keys.setdefault(f"s{u}", self.user_key(u))
        sh(["docker", "rm", "-f", name])
        rc, out = sh(["docker", "run", "-d", "--name", name, "--memory", "96m",
                      "--add-host", f"hs-stress.test:{self.ip}", "-e", "TS_USERSPACE=true", "-e", f"TS_AUTHKEY={key}",
                      "-e", f"TS_EXTRA_ARGS=--login-server={LOGIN}", "-e", f"TS_HOSTNAME={name}",
                      "-e", "TS_STATE_DIR=/var/lib/tailscale", "-e", "TS_ACCEPT_DNS=false",
                      "-e", "SSL_CERT_FILE=/certs/ca.crt", "-v", f"{HERE}/certs:/certs:ro", TS_IMAGE])
        self.started[name] = time.monotonic()
        if rc:
            self.event("student start failed", name, out[-200:])

    def mem_available_mb(self) -> int:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable"):
                return int(line.split()[1]) // 1024
        return 0

    def wait_online(self, want: set[str], what: str, timeout: float = 600) -> float | None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            names = {self.host(n) for n in self.nodes() if n.get("online")}
            if want <= names:
                return time.monotonic() - t0
            time.sleep(3)
        missing = sorted(want - names)
        self.event(f"{what}: not all online", len(missing), " ".join(missing[:10]))
        return None

    # -- background samplers ----------------------------------------------------------------------------
    def sample_resources(self) -> None:
        while not self.stop.is_set():
            rc, out = sh(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=60)
            groups: dict[str, list] = {}
            for line in out.splitlines():
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                name = s["Name"]
                kind = ("pcs" if name.startswith("pc-") else "students" if name[:1] == "s" and "-" in name
                        and name[1:3].isdigit() else name)
                cpu = float(s["CPUPerc"].rstrip("%") or 0)
                used = s["MemUsage"].split("/")[0].strip()
                mb = float(used[:-3]) * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}.get(used[-3:], 1) if used[-3:] in (
                    "KiB", "MiB", "GiB") else 0
                groups.setdefault(kind, []).append((cpu, mb))
            for kind, vals in sorted(groups.items()):
                self.row("resources", [iso(), self.phase, kind, len(vals), round(sum(v[0] for v in vals), 1),
                                       round(sum(v[1] for v in vals), 1)])
            load = open("/proc/loadavg").read().split()[0]
            self.row("resources", [iso(), self.phase, "host-ct", "", load, self.mem_available_mb()])
            self.stop.wait(10)

    def watch_agent(self) -> None:
        while not self.stop.is_set():
            try:
                db = sqlite3.connect(f"file:{HUB_DB}?mode=ro", uri=True, timeout=10)
                r = db.execute("SELECT report FROM servers WHERE name = 'hs1'").fetchone()
                db.close()
                rep = json.loads(r[0]) if r and r[0] else {}
            except Exception:  # noqa: BLE001
                rep = {}
            if rep.get("at") and rep["at"] != self.last_report_at:
                self.last_report_at = rep["at"]
                rep["_seen"] = time.monotonic()
                self.reports.append(rep)
                errs = rep.get("errors") or []
                self.row("agent_runs", [rep["at"], self.phase, rep.get("run_seconds"), rep.get("ssh_seconds"),
                                        rep.get("policy_changed"), len(rep.get("deleted") or []),
                                        len(rep.get("would_delete") or []), len(rep.get("ssh") or []), len(errs),
                                        (errs[0] if errs else "")[:200]])
            self.stop.wait(2)

    def manage_students(self) -> None:
        """Students whose device was removed sign in again 10-60 s after their group's slot starts;
        students whose group is out stay away (as real ones would)."""
        while not self.stop.is_set():
            try:
                active = set(self.assign.values())
                rc, out = sh(["docker", "ps", "-a", "--filter", "name=^s[0-9]", "--format", "{{.Names}} {{.State}}"])
                state = dict(line.split() for line in out.splitlines() if " " in line)
                registered = {self.host(n) for n in self.nodes()}
                for name, st in state.items():
                    u = int(name[1:3])
                    if st == "running" and time.monotonic() - self.started.get(name, 0) < 45:
                        continue  # just started: give it time to register
                    gone = st != "running" or name not in registered
                    if not gone:
                        self.relogin_due.pop(name, None)
                        continue
                    if group_of(u) in active or not self.cleanup:
                        due = self.relogin_due.setdefault(name, time.monotonic() + rnd.uniform(10, 60))
                        if time.monotonic() >= due:
                            self.relogin_due.pop(name, None)
                            self.run_user(u, name[-1])
                            self.event("student signed in again", name)
                    elif st == "running" and name not in registered:
                        sh(["docker", "stop", "-t", "1", name])  # removed and out of slot: the student leaves
            except Exception as e:  # noqa: BLE001
                self.event("student manager error", "", str(e)[:200])
            self.stop.wait(5)

    def probe(self, name: str, ip: str) -> tuple[str, int]:
        t0 = time.monotonic()
        rc, out = sh(["docker", "exec", name, "sh", "-c",
                      f"sleep 2 | timeout 6 tailscale --socket=/tmp/tailscaled.sock nc {ip} 22 2>/dev/null | head -c 7"],
                     timeout=20)
        ms = round((time.monotonic() - t0) * 1000) - 2000
        return ("open" if out.startswith("SSH-") else "blocked"), max(ms, 0)

    def probe_loop(self) -> None:
        while not self.stop.is_set():
            t_round = time.monotonic()
            try:
                nodes = self.nodes()
                ips = self.pc_ips(nodes)
                online = {self.host(n) for n in nodes if n.get("online")}
                assign = dict(self.assign)
                jobs = []
                for name in sorted(online):
                    if not (name[:1] == "s" and name[1:3].isdigit()):
                        continue
                    g = group_of(int(name[1:3]))
                    mine = [k for k, gg in assign.items() if gg == g and k in ips]
                    other = [k for k in ips if assign.get(k) != g]
                    if mine:
                        jobs.append((name, g, rnd.choice(mine), "allowed"))
                    if other:
                        jobs.append((name, g, rnd.choice(other), "blocked"))
                with ThreadPoolExecutor(max_workers=24) as pool:
                    results = list(pool.map(lambda j: self.probe(j[0], ips[j[2]]), jobs))
                t_change = self.t_since_change()
                ok = wrong_open = wrong_blocked = 0
                for (name, g, k, expected), (result, ms) in zip(jobs, results):
                    self.row("probes", [iso(), self.phase, t_change, name, f"G{g:02d}", f"pc-{k}", expected, result, ms])
                    if (expected == "allowed") == (result == "open"):
                        ok += 1
                    elif expected == "blocked":
                        wrong_open += 1
                    else:
                        wrong_blocked += 1
                self.probe_rounds.append({"t": time.monotonic(), "start": t_round, "n": len(jobs), "ok": ok,
                                          "leaks": wrong_open, "denied": wrong_blocked, "phase": self.phase})
            except Exception as e:  # noqa: BLE001
                self.event("probe error", "", str(e)[:200])
            self.stop.wait(max(1, 20 - (time.monotonic() - t_round)))

    def pages(self, times: int = 3) -> None:
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        op.open("http://127.0.0.1:3100/slots/dev-login?email=admin@stress.test", timeout=30).read()
        for _ in range(times):
            for page in ("/", "/calendar?view=day", "/calendar?view=week", "/calendar?view=month",
                         "/calendar?view=quarter", "/pcs", "/settings", "/audit"):
                t0 = time.monotonic()
                try:
                    r = op.open(f"http://127.0.0.1:3100/slots{page}", timeout=60)
                    r.read()
                    status = r.status
                except Exception as e:  # noqa: BLE001
                    status = str(e)[:40]
                self.row("pages", [iso(), self.phase, page, status, round((time.monotonic() - t0) * 1000)])

    # -- one slot change, measured --------------------------------------------------------------------
    def slot_change(self, label: str, cleanup: bool, timeout: float = 300) -> None:
        self.start_phase(f"slot change {label}")
        if cleanup != self.cleanup:
            setting("device_cleanup", "1" if cleanup else "0")
            self.cleanup = cleanup
            self.event("device clean-up", "on" if cleanup else "off")
        self.assign = ASSIGN[label]
        n_reports, n_rounds = len(self.reports), len(self.probe_rounds)
        self.t_change = t0 = time.monotonic()
        set_slot(self.assign)
        self.event("schedule changed", f"{len(self.assign)} PCs busy, {len(set(self.assign.values()))} groups in")
        want_acc = {k: f"grp-g{g:02d}" for k, g in self.assign.items()}
        marks = {}
        while time.monotonic() - t0 < timeout:
            for rep in self.reports[n_reports:]:
                if "policy" not in marks and rep.get("policy_changed"):
                    marks["policy"] = rep["_seen"] - t0
                    self.event("policy applied", round(marks["policy"], 1), "s after the change (incl. agent's 15 s poll)")
                    self.add_summary(f"slot change {label}: policy applied", round(marks["policy"], 1), "s")
                if rep.get("deleted") and "removed" not in marks:
                    marks["removed"] = rep["_seen"] - t0
                    self.event("devices removed", len(rep["deleted"]), f"at {marks['removed']:.1f} s")
                    self.add_summary(f"slot change {label}: devices removed", len(rep["deleted"]), "devices")
                if rep.get("ssh_seconds") and "ssh" not in marks:
                    marks["ssh"] = rep["ssh_seconds"]
                    self.event("PC switching round", rep["ssh_seconds"], f"s for {len(rep.get('ssh') or [])} PCs")
                    self.add_summary(f"slot change {label}: SSH switching time", rep["ssh_seconds"], "s")
                    self.add_summary(f"slot change {label}: PCs switched", len(rep.get("ssh") or []), "PCs")
                st = rep.get("pc_state") or {}
                done = sum(1 for k in range(1, N_PCS + 1)
                           if (st.get(str(k)) or {}).get("active_account") == want_acc.get(k, "-"))
                if "allpcs" not in marks and done == N_PCS:
                    marks["allpcs"] = rep["_seen"] - t0
                    self.event("all PCs on the new account", round(marks["allpcs"], 1), "s")
                    self.add_summary(f"slot change {label}: all 70 PCs switched", round(marks["allpcs"], 1), "s")
            for rnd_ in self.probe_rounds[n_rounds:]:
                if rnd_["start"] < t0 + (marks.get("policy", 9e9)):
                    continue
                if rnd_["n"] and rnd_["ok"] == rnd_["n"] and "correct" not in marks:
                    marks["correct"] = rnd_["t"] - t0
                    self.event("every student probe correct", round(marks["correct"], 1), f"s ({rnd_['n']} probes)")
                    self.add_summary(f"slot change {label}: all students see the new schedule",
                                     round(marks["correct"], 1), "s")
            if self.cleanup and "back" not in marks and int(time.monotonic() - t0) % 6 < 2:
                online = {self.host(n) for n in self.nodes() if n.get("online")}
                active = set(self.assign.values())
                want = {f"s{u:02d}-a" for u in range(1, N_USERS + 1) if group_of(u) in active}
                if want <= online:
                    marks["back"] = time.monotonic() - t0
                    self.event("all incoming students online", round(marks["back"], 1), f"s ({len(want)} students)")
                    self.add_summary(f"slot change {label}: incoming students all signed in",
                                     round(marks["back"], 1), "s")
            need = {"policy", "allpcs", "correct"} | ({"back"} if self.cleanup else set())
            if need <= marks.keys():
                break
            time.sleep(2)
        rounds = [r for r in self.probe_rounds[n_rounds:]]
        leaks = sum(r["leaks"] for r in rounds if r["start"] > t0 + marks.get("policy", 0) + 5)
        self.add_summary(f"slot change {label}: wrong access after the policy (leaks)", leaks, "probes")
        for key in ("policy", "allpcs", "correct") + (("back",) if self.cleanup else ()):
            if key not in marks:
                self.event(f"{key}: not reached within {timeout:.0f} s")

    def hold(self, seconds: float, name: str) -> None:
        self.start_phase(name)
        time.sleep(seconds)

    def churn(self, seconds: float) -> None:
        self.start_phase("churn")
        t0, n = time.monotonic(), 0
        while time.monotonic() - t0 < seconds:
            rc, out = sh(["docker", "ps", "--filter", "name=^s[0-9]", "--format", "{{.Names}}"])
            names = out.split()
            if names:
                sh(["docker", "restart", "-t", "1", rnd.choice(names)])
                n += 1
            time.sleep(3)
        self.event("student reconnects", n, f"in {seconds:.0f} s")
        self.add_summary("churn: student reconnects", n, "reconnects")

    def phase_resources(self) -> None:
        """Mean and peak CPU/memory per container group, per phase, into summary.csv."""
        rows = list(csv.DictReader(open(self.dir / "resources.csv")))
        for phase in dict.fromkeys(r["phase"] for r in rows):
            for kind in ("stress-hs", "stress-hub", "stress-agent", "pcs", "students"):
                vals = [r for r in rows if r["phase"] == phase and r["container"] == kind]
                if not vals:
                    continue
                cpu = [float(r["cpu_pct"]) for r in vals]
                mem = [float(r["mem_mb"]) for r in vals]
                label = {"stress-hs": "Headscale", "stress-hub": "website", "stress-agent": "agent"}.get(kind, kind)
                self.summary.append([phase, f"{label} CPU mean", round(statistics.mean(cpu), 1), "% of one core"])
                self.summary.append([phase, f"{label} CPU peak", round(max(cpu), 1), "% of one core"])
                self.summary.append([phase, f"{label} memory peak", round(max(mem), 1), "MB"])
        pages = list(csv.DictReader(open(self.dir / "pages.csv")))
        for page in dict.fromkeys(p["page"] for p in pages):
            ms = [int(p["ms"]) for p in pages if p["page"] == page]
            self.summary.append(["pages", f"{page} load, median", statistics.median(ms), "ms"])
        api = [int(r["ms"]) for r in csv.DictReader(open(self.dir / "api.csv"))]
        if api:
            self.summary.append(["all", "Headscale API list-nodes, median", statistics.median(api), "ms"])
            self.summary.append(["all", "Headscale API list-nodes, p95", sorted(api)[int(len(api) * .95) - 1], "ms"])

    # -- the whole run ----------------------------------------------------------------------------------
    def main(self) -> None:
        (self.dir / "environment.csv").write_text(
            "key,value\n"
            f"cpu_cores,{sh(['nproc'])[1]}\n"
            f"memory_mb,{self.mem_available_mb()}\n"
            f"headscale,{sh(['docker', 'exec', 'stress-hs', 'headscale', 'version'])[1].splitlines()[0]}\n"
            f"tailscale,{TS_IMAGE}\n"
            f"pcs,{N_PCS}\ngroups,{N_GROUPS}\nstudents,{N_USERS}\nagent_poll_s,15\nprobe_round_s,20\n")
        for target in (self.sample_resources, self.watch_agent):
            threading.Thread(target=target, daemon=True).start()

        self.start_phase("PCs join")
        for batch in range(0, N_PCS, 10):
            for k in range(batch + 1, min(batch + 10, N_PCS) + 1):
                self.run_pc(k)
            time.sleep(2)
        t = self.wait_online({f"pc-{k}" for k in range(1, N_PCS + 1)}, "PCs")
        if t is not None:
            self.event("all PCs online", round(t, 1), "s after the last batch started")
        self.add_summary("PCs online", round(t, 1) if t else "not all", "s")

        self.start_phase("students join")
        t0 = time.monotonic()
        for batch in range(0, N_USERS, 10):
            if self.mem_available_mb() < 700:
                self.event("stopped adding students: low memory", self.mem_available_mb(), "MB available")
                break
            for u in range(batch + 1, min(batch + 10, N_USERS) + 1):
                self.run_user(u)
            time.sleep(2)
        t = self.wait_online({f"s{u:02d}-a" for u in range(1, N_USERS + 1)}, "students")
        if t is not None:
            self.event("all students online", round(time.monotonic() - t0, 1), "s from first start")
        self.add_summary("students online", round(time.monotonic() - t0, 1) if t is not None else "not all", "s")
        for target in (self.probe_loop, self.manage_students):
            threading.Thread(target=target, daemon=True).start()

        self.hold(120, "steady (150 devices)")
        self.pages(3)
        self.slot_change("B", cleanup=False)
        self.churn(180)
        self.slot_change("C", cleanup=True)
        self.slot_change("D", cleanup=True, timeout=360)

        self.start_phase("second device per student")
        for batch in range(0, N_USERS, 10):
            if self.mem_available_mb() < 900:
                self.event("stopped adding devices: low memory", self.mem_available_mb(), "MB available")
                break
            for u in range(batch + 1, min(batch + 10, N_USERS) + 1):
                if group_of(u) in set(self.assign.values()):
                    self.run_user(u, "b")
            time.sleep(3)
        time.sleep(60)
        n = sum(1 for nd in self.nodes() if nd.get("online"))
        self.event("devices online", n)
        self.add_summary("devices online at peak", n, "devices")
        self.hold(90, "steady (peak devices)")
        self.pages(2)
        self.slot_change("E", cleanup=True, timeout=360)
        self.hold(60, "cool down")

        self.stop.set()
        time.sleep(3)
        self.phase_resources()
        with open(self.dir / "summary.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["phase", "metric", "value", "unit"])
            w.writerows(self.summary)
        for f in self.files.values():
            f.close()
        print(f"results in {self.dir}")


if __name__ == "__main__":
    if sys.argv[1:] == ["seed"]:
        seed()
    else:
        Run().main()
