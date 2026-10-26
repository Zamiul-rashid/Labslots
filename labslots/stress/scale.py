#!/usr/bin/env python3
"""Multi-host stress run: does Headscale in a 4-core container hold ~1,500 devices that are never removed?

The server side (Headscale, hub, slot agent, scheduler sidecar) runs in the stress CT, built by
`FRONT=<ip> RESET=1 SEED=scale ./setup.sh`. This controller runs on a workstation and drives:
  * the 70 lab PCs: Docker containers here (Tailscale SSH, the real setup-lab-pc.sh)
  * the students: plain tailscaled processes on every device machine (devhost.py), two devices each
  * fwd.py on this machine: the front address every device uses, like the reverse proxy in production

Device clean-up stays off the whole run: every student keeps both devices online, in slot or not.

    python3 scale.py prepare     copy devhost.py, the Tailscale binaries and the CA to the device machines
    python3 scale.py run         every phase; CSVs to results/<run>/
    python3 scale.py teardown    stop every device and PC here and on the device machines, delete their files
"""

from __future__ import annotations

import csv
import http.cookiejar
import json
import os
import random
import shlex
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DHAKA = ZoneInfo("Asia/Dhaka")
WORK = Path(os.environ.get("LSX_WORK", Path(tempfile.gettempdir()) / "lsx-stress"))  # binaries and CA, this machine
CM = Path(tempfile.gettempdir()) / f"lsx-cm-{os.getuid()}"  # ssh control sockets (a short path: sockets max 108 chars)
FRONT = "https://10.0.0.10:8443"
CORE = ["ssh", "proxmox", "pct", "exec", "190", "--", "python3", "/root/stress/corectl.py"]
HUB = "http://10.0.0.20:3100/slots"
TS_IMAGE = "tailscale/tailscale:v1.102.3"
N_PCS, N_GROUPS, N_USERS, DEVICES_PER_USER, BUSY = 70, 140, 700, 2, 65
# Device machines. floor_mb: free memory each must keep (host-c also runs a homelab); no new devices below it.
HOSTS = [
    {"name": "host-a", "ssh": None, "floor_mb": 2500},
    {"name": "host-c", "ssh": "user@host-c", "floor_mb": 4000},
    {"name": "host-b", "ssh": "user@host-b", "floor_mb": 1200},
]
ASSIGN = {  # pc -> group for the slot running now. C replaces every group (the biggest turnover a slot change has)
    "A": {k: k for k in range(1, BUSY + 1)},
    "B": {k: (k - 1 + 19) % N_GROUPS + 1 for k in range(1, BUSY + 1)},
    "C": {k: k + N_PCS for k in range(1, BUSY + 1)},
    "E": {k: (k - 1 + 7) % N_GROUPS + 1 for k in range(1, BUSY + 1)},
}
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ControlMaster=auto",
            "-o", f"ControlPath={CM}/%C", "-o", "ControlPersist=90m"]
rnd = random.Random(7)


def now() -> datetime:
    return datetime.now(DHAKA).replace(tzinfo=None)


def iso() -> str:
    return now().isoformat(timespec="seconds")


def run(args: list[str], stdin: str | None = None, timeout: float = 600) -> tuple[int, str]:
    try:
        p = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip() + (("\n" + p.stderr.strip()) if p.returncode else "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def ssh(target: str, *cmd: str) -> list[str]:
    """ssh joins its arguments into one remote shell line, so each one is quoted here."""
    return ["ssh", *SSH_OPTS, target, shlex.join(cmd) if len(cmd) > 1 else cmd[0]]


def core(*args: str, timeout: float = 900):
    rc, out = run(ssh(CORE[1], *CORE[2:], *args), timeout=timeout)
    if rc:
        raise RuntimeError(f"corectl {args[0]}: {out[-300:]}")
    return json.loads(out.splitlines()[-1])


def dev(host: dict, *args: str, stdin=None, timeout: float = 300):
    cmd = ["python3", "-I", "lsx-stress/devhost.py", *args]
    full = ssh(host["ssh"], *cmd) if host["ssh"] else ["bash", "-c", "cd ~ && " + shlex.join(cmd)]
    rc, out = run(full, json.dumps(stdin) if stdin is not None else None, timeout)
    if rc:
        raise RuntimeError(f"{host['name']} devhost {args[0]}: {out[-300:]}")
    return json.loads(out.splitlines()[-1])


# ------------------------------------------------------------------------------------- prepare / teardown
def prepare() -> None:
    WORK.mkdir(exist_ok=True)
    rc, ca = run(ssh("proxmox", "pct", "exec", "190", "--", "cat", "/root/stress/certs/ca.crt"))
    assert rc == 0 and "BEGIN CERTIFICATE" in ca, ca
    (WORK / "ca.crt").write_text(ca + "\n")
    if not (WORK / "bin" / "tailscaled").exists():
        (WORK / "bin").mkdir(exist_ok=True)
        rc, cid = run(["docker", "create", TS_IMAGE])
        for b in ("tailscale", "tailscaled"):
            run(["docker", "cp", "-q", f"{cid}:/usr/local/bin/{b}", str(WORK / "bin" / b)])
        run(["docker", "rm", cid])
    for h in HOSTS:
        tar = ["tar", "czf", "-", "-C", str(WORK), "bin", "ca.crt", "-C", str(HERE), "devhost.py"]
        unpack = "mkdir -p ~/lsx-stress && tar xzf - -C ~/lsx-stress && ~/lsx-stress/bin/tailscale version | head -1"
        p1 = subprocess.Popen(tar, stdout=subprocess.PIPE)
        target = ssh(h["ssh"], unpack) if h["ssh"] else ["bash", "-c", unpack]
        out = subprocess.run(target, stdin=p1.stdout, capture_output=True, text=True, timeout=300)
        print(f"{h['name']}: ~/lsx-stress ready, tailscale {out.stdout.strip()}", out.stderr.strip()[-200:])
    pc_dir = HERE / "pc"
    (pc_dir / "setup-lab-pc.sh").write_text((HERE.parent / "labslots" / "pc" / "setup-lab-pc.sh").read_text())
    rc, out = run(["docker", "build", "-q", "-t", "lsx-pc", str(pc_dir)], timeout=900)
    print("PC image:", "built" if rc == 0 else out[-300:])


def teardown() -> None:
    rc, out = run(["bash", "-c", "docker ps -aq --filter name=^lsx-pc- | xargs -r docker rm -f | wc -l"])
    print(f"host-a: removed {out} PC containers")
    run(["docker", "rmi", "-f", "lsx-pc"])
    for h in HOSTS:
        try:
            print(h["name"], dev(h, "teardown"))
        except Exception as e:  # noqa: BLE001
            print(h["name"], "teardown:", e)


# ------------------------------------------------------------------------------------------------ the run
class Run:
    def __init__(self) -> None:
        self.dir = HERE / "results" / ("scale-" + now().strftime("%Y%m%d-%H%M%S"))
        self.dir.mkdir(parents=True)
        self.lock = threading.Lock()
        self.files, self.writers = {}, {}
        for name, cols in (
                ("events", ["time", "phase", "t_phase_s", "t_change_s", "event", "value", "detail"]),
                ("core", ["time", "phase", "container", "cpu_pct", "mem_mb", "ct_load1", "ct_mem_available_mb"]),
                ("hosts", ["time", "phase", "host", "devices", "rss_mb", "cpu_pct", "mem_available_mb", "load1"]),
                ("agent_runs", ["time", "phase", "run_s", "ssh_s", "policy_changed", "switched", "errors",
                                "first_error"]),
                ("probes", ["time", "phase", "t_change_s", "device", "host", "group", "pc", "expected", "ok", "ms",
                            "out"]),
                ("api", ["time", "phase", "ms", "nodes", "online"]),
                ("pages", ["time", "phase", "page", "status", "ms"])):
            f = open(self.dir / f"{name}.csv", "w", newline="")
            self.files[name], self.writers[name] = f, csv.writer(f)
            self.writers[name].writerow(cols)
        self.summary: list[list] = []
        self.phase, self.t_phase, self.t_change = "setup", time.monotonic(), None
        self.assign = ASSIGN["A"]
        self.stop = threading.Event()
        self.where: dict[str, dict] = {}            # device name -> host
        self.reports: list[dict] = []
        self.last_report_at = ""
        self.rounds: list[dict] = []
        self.ssl = ssl.create_default_context(cafile=str(WORK / "ca.crt"))
        self.ssl.verify_flags &= ~ssl.VERIFY_X509_STRICT  # the throwaway test CA has no keyUsage (Go clients accept it)
        self.api_key = core("apikey")["key"]
        self._nodes, self._nodes_at = [], 0.0

    # -- recording --------------------------------------------------------------------------------------
    def row(self, name: str, values: list) -> None:
        with self.lock:
            self.writers[name].writerow(values)
            self.files[name].flush()

    def t_change_s(self) -> str:
        return f"{time.monotonic() - self.t_change:.1f}" if self.t_change else ""

    def event(self, name: str, value="", detail: str = "") -> None:
        self.row("events", [iso(), self.phase, f"{time.monotonic() - self.t_phase:.1f}", self.t_change_s(), name,
                            value, detail])
        print(f"[{iso()[11:]}] {self.phase:<24} {name} {value} {detail}", flush=True)

    def start_phase(self, name: str) -> None:
        self.phase, self.t_phase = name, time.monotonic()
        self.event("phase started")

    def add(self, metric: str, value, unit: str = "", phase: str | None = None) -> None:
        self.summary.append([phase or self.phase, metric, value, unit])

    # -- Headscale API (cached for 5 s, so the harness adds little load of its own) ------------------------
    def nodes(self, fresh: bool = False) -> list[dict]:
        if not fresh and time.monotonic() - self._nodes_at < 5:
            return self._nodes
        t0 = time.monotonic()
        req = urllib.request.Request(f"{FRONT}/api/v1/node", headers={"Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, context=self.ssl, timeout=60) as r:
            nodes = json.load(r).get("nodes", [])
        self.row("api", [iso(), self.phase, round((time.monotonic() - t0) * 1000), len(nodes),
                         sum(1 for n in nodes if n.get("online"))])
        self._nodes, self._nodes_at = nodes, time.monotonic()
        return nodes

    @staticmethod
    def name_of(n: dict) -> str:
        return n.get("givenName") or n.get("given_name") or n.get("name") or ""

    def online(self) -> set[str]:
        return {self.name_of(n) for n in self.nodes() if n.get("online")}

    def pc_ips(self) -> dict[int, str]:
        out = {}
        for n in self.nodes():
            ips = [i for i in (n.get("ipAddresses") or n.get("ip_addresses") or []) if "." in i]
            for t in n.get("tags") or []:
                if t.startswith("tag:pc-") and ips and n.get("online"):
                    out[int(t[7:])] = ips[0]
        return out

    def wait_online(self, want: set[str], what: str, timeout: float) -> float | None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            got = want & self.online()
            if got == want:
                return time.monotonic() - t0
            time.sleep(5)
        missing = sorted(want - self.online())
        self.event(f"{what}: not all online", len(missing), " ".join(missing[:8]))
        return None

    # -- samplers -----------------------------------------------------------------------------------------
    def sample_core(self) -> None:
        while not self.stop.is_set():
            try:
                s = core("stats", timeout=60)
                for name, c in s["containers"].items():
                    self.row("core", [iso(), self.phase, name, c["cpu_pct"], c["mem_mb"], s["load1"],
                                      s["mem_available_mb"]])
            except Exception as e:  # noqa: BLE001
                self.event("core stats error", "", str(e)[:200])
            self.stop.wait(8)

    def sample_hosts(self) -> None:
        last: dict[str, tuple[float, float]] = {}
        while not self.stop.is_set():
            for h in HOSTS:
                try:
                    s = dev(h, "stats", timeout=60)
                    t = time.monotonic()
                    cpu = ""
                    if h["name"] in last:
                        t1, c1 = last[h["name"]]
                        cpu = round((s["cpu_s"] - c1) / max(t - t1, 1) * 100, 1)
                    last[h["name"]] = (t, s["cpu_s"])
                    h["avail"] = s["mem_available_mb"]
                    self.row("hosts", [iso(), self.phase, h["name"], s["devices"], s["rss_mb"], cpu,
                                       s["mem_available_mb"], s["load1"]])
                except Exception as e:  # noqa: BLE001
                    self.event("host stats error", h["name"], str(e)[:200])
            self.stop.wait(15)

    def watch_agent(self) -> None:
        while not self.stop.is_set():
            try:
                rep = core("report", timeout=60)
            except Exception:  # noqa: BLE001
                rep = {}
            if rep.get("at") and rep["at"] != self.last_report_at:
                self.last_report_at = rep["at"]
                rep["_seen"] = time.monotonic()
                self.reports.append(rep)
                errs = rep.get("errors") or []
                self.row("agent_runs", [rep["at"], self.phase, rep.get("run_seconds"), rep.get("ssh_seconds"),
                                        rep.get("policy_changed"), len(rep.get("ssh") or []), len(errs),
                                        (errs[0] if errs else "")[:200]])
            self.stop.wait(3)

    def probe_loop(self, per_round: int = 72) -> None:
        """Every 30 s: sampled students try Tailscale SSH logins: to their group's PC as their group's account
        (should work during the slot), to it as another group's account, and to another PC (both refused)."""
        while not self.stop.is_set():
            t_round = time.monotonic()
            try:
                ips, online, assign = self.pc_ips(), self.online(), dict(self.assign)
                pc_of = {}
                for k, g in assign.items():
                    pc_of.setdefault(g, []).append(k)
                names = [n for n in online if n in self.where]
                jobs: dict[str, list] = {}
                for name in rnd.sample(names, min(per_round, len(names))):
                    u = int(name[1:4])
                    g = (u - 1) % N_GROUPS + 1
                    acct = f"grp-g{g:03d}"
                    mine = [k for k in pc_of.get(g, []) if k in ips]
                    other = [k for k in ips if assign.get(k) != g]
                    h = self.where[name]["name"]
                    if mine:  # its own PC: in as its own account, not as another group's
                        k = rnd.choice(mine)
                        jobs.setdefault(h, []).append((name, g, k, "allowed", acct))
                        jobs.setdefault(h, []).append((name, g, k, "other account", f"grp-g{g % N_GROUPS + 1:03d}"))
                    if other:
                        jobs.setdefault(h, []).append((name, g, rnd.choice(other), "blocked", acct))
                t_change = self.t_change_s()
                ok = leaks = denied = n = 0

                def on_host(item):
                    h, js = item
                    host = next(x for x in HOSTS if x["name"] == h)
                    res = dev(host, "probe", stdin=[{"name": j[0], "ip": ips[j[2]], "user": j[4],
                                                     "timeout": 15 if j[3] == "allowed" else 6} for j in js], timeout=180)
                    return h, js, res
                with ThreadPoolExecutor(len(HOSTS)) as pool:
                    for h, js, res in pool.map(on_host, jobs.items()):
                        for (name, g, k, expected, _), r in zip(js, res):
                            self.row("probes", [iso(), self.phase, t_change, name, h, f"G{g:03d}", f"pc-{k}", expected,
                                                r["ok"], r["ms"], r["out"].replace("\n", " ")[:120]])
                            n += 1
                            if r["ok"] == (expected == "allowed"):
                                ok += 1
                            elif expected != "allowed":
                                leaks += 1
                            else:
                                denied += 1
                self.rounds.append({"start": t_round, "end": time.monotonic(), "n": n, "ok": ok, "leaks": leaks,
                                    "denied": denied})
            except Exception as e:  # noqa: BLE001
                self.event("probe error", "", str(e)[:200])
            self.stop.wait(max(1, 30 - (time.monotonic() - t_round)))

    def pages(self, times: int = 2) -> None:
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        op.open(f"{HUB}/dev-login?email=admin@stress.test", timeout=30).read()
        for _ in range(times):
            for page in ("/", "/calendar?view=week", "/calendar?view=month", "/pcs", "/settings", "/audit"):
                t0 = time.monotonic()
                try:
                    r = op.open(f"{HUB}{page}", timeout=120)
                    r.read()
                    status = r.status
                except Exception as e:  # noqa: BLE001
                    status = str(e)[:40]
                self.row("pages", [iso(), self.phase, page, status, round((time.monotonic() - t0) * 1000)])

    # -- phases -------------------------------------------------------------------------------------------
    def pcs_join(self) -> None:
        self.start_phase("PCs join")
        keys = core("pckeys", str(N_PCS))
        run(["bash", "-c", "docker ps -aq --filter name=^lsx-pc- | xargs -r docker rm -f"])

        def start(k: int):
            return run(["docker", "run", "-d", "--name", f"lsx-pc-{k}", "--hostname", f"pc-{k}", "--memory", "128m",
                        "-e", f"TS_LOGIN={FRONT}", "-e", f"TS_AUTHKEY={keys[str(k)]}", "-e", f"TS_HOSTNAME=pc-{k}",
                        "-e", "SSL_CERT_FILE=/certs/ca.crt", "-v", f"{WORK}/ca.crt:/certs/ca.crt:ro", "lsx-pc"])
        with ThreadPoolExecutor(10) as pool:
            for k, (rc, out) in zip(range(1, N_PCS + 1), pool.map(start, range(1, N_PCS + 1))):
                if rc:
                    self.event("PC start failed", k, out[-200:])
        t = self.wait_online({f"pc-{k}" for k in range(1, N_PCS + 1)}, "PCs", 600)
        self.add("PCs online", round(t, 1) if t is not None else "not all", "s")

    def students_join(self, target: int) -> None:
        self.start_phase("students join")
        keys = core("userkeys", str(N_USERS), timeout=1800)
        self.event("student keys ready", len(keys))
        devices = [(u, d) for d in "ab"[:DEVICES_PER_USER] for u in range(1, N_USERS + 1)][:target]
        t0, i = time.monotonic(), 0
        while i < len(devices):
            for h in HOSTS:
                h["avail"] = dev(h, "stats")["mem_available_mb"]
            room = [h for h in HOSTS if h["avail"] - 200 > h["floor_mb"]]
            if not room:
                self.event("stopped adding devices: every machine at its memory floor", i)
                break
            for h in room:
                batch = devices[i:i + 25]
                if not batch:
                    break
                i += len(batch)
                specs = [{"name": f"s{u:03d}-{d}", "key": keys[str(u)], "login": FRONT} for u, d in batch]
                dev(h, "start", stdin=specs)
                for s in specs:
                    self.where[s["name"]] = h
            time.sleep(4)
        self.event("devices started", len(self.where), " ".join(f"{h['name']}={sum(1 for x in self.where.values() if x is h)}"
                                                               for h in HOSTS))
        t = self.wait_online(set(self.where), "students", 900)
        self.add("student devices started", len(self.where), "devices")
        self.add("all student devices online", round(time.monotonic() - t0, 1) if t is not None else "not all", "s")
        n = len(self.online())
        self.event("devices online", n)
        self.add("devices online (PCs, students, scheduler)", n, "devices")

    def slot_change(self, label: str, timeout: float = 420) -> None:
        self.start_phase(f"slot change {label}")
        self.assign = ASSIGN[label]
        want = {k: f"grp-g{g:03d}" for k, g in self.assign.items()}
        n_rep, n_rounds = len(self.reports), len(self.rounds)
        self.t_change = t0 = time.monotonic()
        core("slot", json.dumps(self.assign))
        self.event("schedule changed", f"{len(self.assign)} PCs busy, {len(set(self.assign.values()))} groups")
        marks: dict[str, float] = {}
        while time.monotonic() - t0 < timeout:
            for rep in self.reports[n_rep:]:
                if "policy" not in marks and rep.get("policy_changed"):
                    marks["policy"] = rep["_seen"] - t0
                    self.event("policy applied", round(marks["policy"], 1), f"s (agent run {rep.get('run_seconds')} s)")
                st = rep.get("pc_state") or {}
                done = sum(1 for k in range(1, N_PCS + 1)
                           if (st.get(str(k)) or {}).get("active_account") == want.get(k, "-"))
                if "pcs" not in marks and done == N_PCS:
                    marks["pcs"] = rep["_seen"] - t0
                    self.event("all PCs on the new account", round(marks["pcs"], 1), "s")
            for r in self.rounds[n_rounds:]:
                if "pcs" in marks and r["start"] > t0 + marks["pcs"] and r["n"] and r["ok"] == r["n"] \
                        and "correct" not in marks:
                    marks["correct"] = r["end"] - t0
                    self.event("every probe correct", round(marks["correct"], 1), f"s ({r['n']} probes)")
            if {"policy", "pcs", "correct"} <= marks.keys():
                break
            time.sleep(2)
        for key, metric in (("policy", "policy applied"), ("pcs", "all 70 PCs switched (Tailscale SSH)"),
                            ("correct", "every student probe correct")):
            self.add(f"slot change {label}: {metric}", round(marks[key], 1) if key in marks else "not reached", "s")
        after = [r for r in self.rounds[n_rounds:] if r["start"] > t0 + marks.get("policy", 9e9) + 5]
        self.add(f"slot change {label}: wrong access after the policy (leaks)", sum(r["leaks"] for r in after),
                 "probes")
        self.t_change = None

    def force(self) -> None:
        self.start_phase("re-apply now")
        n_rep, t0 = len(self.reports), time.monotonic()
        core("force")
        while time.monotonic() - t0 < 120:
            hit = [r for r in self.reports[n_rep:] if r.get("policy_changed")]
            if hit:
                self.event("policy rewritten", round(hit[0]["_seen"] - t0, 1), f"s (agent run {hit[0].get('run_seconds')} s)")
                self.add("re-apply now: policy rewritten", round(hit[0]["_seen"] - t0, 1), "s")
                self.add("re-apply now: agent run", hit[0].get("run_seconds"), "s")
                return
            time.sleep(2)
        self.add("re-apply now: policy rewritten", "not reached", "s")

    def churn(self, seconds: float) -> None:
        self.start_phase("churn")
        t0, n = time.monotonic(), 0
        names = list(self.where)
        while time.monotonic() - t0 < seconds:
            pick = rnd.sample(names, 3)
            for name in pick:
                dev(self.where[name], "restart", name)
            n += len(pick)
            time.sleep(3)
        self.event("device reboots", n, f"in {seconds:.0f} s")
        self.add("churn: device reboots", n, "reboots")

    def hold(self, seconds: float, name: str) -> None:
        self.start_phase(name)
        time.sleep(seconds)

    def summarise(self) -> None:
        core_rows = list(csv.DictReader(open(self.dir / "core.csv")))
        for phase in dict.fromkeys(r["phase"] for r in core_rows):
            for name, label in (("stress-hs", "Headscale"), ("stress-agent", "agent"), ("stress-hub", "website")):
                vals = [r for r in core_rows if r["phase"] == phase and r["container"] == name]
                if vals:
                    cpu = [float(r["cpu_pct"]) for r in vals]
                    self.summary.append([phase, f"{label} CPU mean", round(statistics.mean(cpu), 1), "% of one core"])
                    self.summary.append([phase, f"{label} CPU peak", round(max(cpu), 1), "% of one core"])
                    self.summary.append([phase, f"{label} memory peak", max(float(r["mem_mb"]) for r in vals), "MB"])
            ct = [r for r in core_rows if r["phase"] == phase and r["container"] == "stress-hs"]
            if ct:
                self.summary.append([phase, "CT load (1 min) peak", max(float(r["ct_load1"]) for r in ct), "of 4 cores"])
                self.summary.append([phase, "CT free memory, lowest", min(int(r["ct_mem_available_mb"]) for r in ct),
                                     "MB of 4096"])
        api = [int(r["ms"]) for r in csv.DictReader(open(self.dir / "api.csv"))]
        if api:
            self.summary.append(["all", "Headscale API list nodes, median", statistics.median(api), "ms"])
            self.summary.append(["all", "Headscale API list nodes, 95th percentile", sorted(api)[int(len(api) * .95) - 1],
                                 "ms"])
        for page in dict.fromkeys(r["page"] for r in csv.DictReader(open(self.dir / "pages.csv"))):
            ms = [int(r["ms"]) for r in csv.DictReader(open(self.dir / "pages.csv")) if r["page"] == page]
            self.summary.append(["pages", f"page {page}, median load", statistics.median(ms), "ms"])
        probes = list(csv.DictReader(open(self.dir / "probes.csv")))
        self.summary.append(["all", "Tailscale SSH probes run", len(probes), "probes"])
        self.summary.append(["all", "probe logins that worked, median time",
                             statistics.median([int(p["ms"]) for p in probes if p["ok"] == "True"] or [0]), "ms"])

    def main(self) -> None:
        WORK.mkdir(exist_ok=True)
        s = core("stats")
        (self.dir / "environment.csv").write_text(
            "key,value\n"
            f"server side,stress CT 190 on proxmox: 4 cores; {s['mem_total_mb']} MB\n"
            f"headscale,v0.29.3\ntailscale,{TS_IMAGE}\n"
            f"front,{FRONT} (fwd.py on host-a)\n"
            f"pcs,{N_PCS} (Docker on host-a)\ngroups,{N_GROUPS}\nstudents,{N_USERS}\n"
            f"devices per student,{DEVICES_PER_USER}\ndevice clean-up,off for the whole run\n"
            + "".join(f"device machine,{h['name']}: {dev(h, 'stats')['cores']} cores, floor {h['floor_mb']} MB\n"
                      for h in HOSTS))
        core("setting", "device_cleanup", "0")
        core("slot", json.dumps(self.assign))
        for target in (self.sample_core, self.sample_hosts, self.watch_agent):
            threading.Thread(target=target, daemon=True).start()
        self.pcs_join()
        self.students_join(N_USERS * DEVICES_PER_USER)
        threading.Thread(target=self.probe_loop, daemon=True).start()
        self.hold(180, "steady (all devices)")
        self.pages()
        self.slot_change("B")
        self.churn(180)
        self.slot_change("C")
        self.force()
        self.slot_change("E")
        self.pages()
        self.hold(90, "cool down")
        self.stop.set()
        time.sleep(3)
        self.summarise()
        with open(self.dir / "summary.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["phase", "metric", "value", "unit"])
            w.writerows(self.summary)
        for f in self.files.values():
            f.close()
        print(f"results in {self.dir}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    WORK.mkdir(exist_ok=True)
    CM.mkdir(mode=0o700, exist_ok=True)
    if cmd == "prepare":
        prepare()
    elif cmd == "run":
        Run().main()
    elif cmd == "teardown":
        teardown()
    else:
        raise SystemExit(__doc__)
