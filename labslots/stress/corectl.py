#!/usr/bin/env python3
"""Server-side helper for the multi-host stress run. Runs inside the stress CT (/root/stress); scale.py calls
it over SSH. Every command prints JSON.

    corectl.py seed <pcs> <groups> <users>   fill the hub's database: PCs, groups, members, a week of bookings
    corectl.py slot '<json {pc: group}>'     the slot running now (and the next) gets this assignment
    corectl.py setting <key> <value>
    corectl.py force                         the website's "Enforce now": every agent re-applies
    corectl.py report                        the slot agent's latest report
    corectl.py pckeys <pcs>                  a reusable pre-auth key per PC (tag:pc-N)
    corectl.py userkeys <users>              a user and a reusable pre-auth key per student
    corectl.py apikey                        the Headscale API key the agent uses
    corectl.py stats                         CPU and memory per container, the CT's load and free memory
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
HUB_DB = HERE / "hub-data" / "labslots.db"
DHAKA = ZoneInfo("Asia/Dhaka")
PALETTE = ("#ffe08a", "#b8e6b0", "#f7c6d9", "#bcdcf5", "#ffd0a6", "#d9ccf2", "#c9e7e1", "#f4d6b8")


def db():
    return sqlite3.connect(HUB_DB, timeout=30)


def hs(*args: str):
    p = subprocess.run(["docker", "exec", "stress-hs", "headscale", *args, "-o", "json"], capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"headscale {' '.join(args)}: {p.stderr.strip()}")
    return json.loads(p.stdout) if p.stdout.strip() else None


def set_setting(c, key: str, value: str) -> None:
    c.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
              (key, value))


def slots_now() -> list[tuple[int, str]]:
    """The slot running now and the next one (both get the assignment, in case a boundary passes mid-run)."""
    d = datetime.now(DHAKA).replace(tzinfo=None)
    if 6 <= d.hour < 18:
        cur = (d.weekday(), "day")
    elif d.hour >= 18:
        cur = (d.weekday(), "night")
    else:
        cur = ((d - timedelta(days=1)).weekday(), "night")
    return [cur, (cur[0], "night") if cur[1] == "day" else ((cur[0] + 1) % 7, "day")]


def seed(n_pcs: int, n_groups: int, n_users: int) -> dict:
    c = db()
    for t in ("bookings", "members", "groups", "pcs", "terms"):
        c.execute(f"DELETE FROM {t}")
    today = date.today()
    c.execute("INSERT INTO terms(id, name, start_date, end_date) VALUES (1, 'Stress term', ?, ?)",
              ((today - timedelta(days=30)).isoformat(), (today + timedelta(days=150)).isoformat()))
    c.executemany("INSERT INTO pcs(id, name, tag, ssh_enabled) VALUES (?, ?, ?, 1)",
                  [(k, f"pc-{k}", f"tag:pc-{k}") for k in range(1, n_pcs + 1)])
    c.executemany("INSERT INTO groups(id, name, slug, linux_account, color, short) VALUES (?, ?, ?, ?, ?, ?)",
                  [(g, f"Group {g:03d}", f"g{g:03d}", f"grp-g{g:03d}", PALETTE[g % len(PALETTE)], f"G{g:03d}")
                   for g in range(1, n_groups + 1)])
    c.executemany("INSERT INTO members(group_id, email) VALUES (?, ?)",
                  [((u - 1) % n_groups + 1, f"s{u:03d}@stress.test") for u in range(1, n_users + 1)])
    rows = []  # a full week, two slots per group per week where it fits, so the calendar has realistic data
    for k in range(1, n_pcs + 1):
        for wd in range(6):
            for h, half in enumerate(("day", "night")):
                rows.append((k, (k * 3 + wd * 7 + h * 29) % n_groups + 1, wd, half))
    c.executemany("INSERT INTO bookings(term_id, pc_id, group_id, weekday, half, status, requested_by, requested_at) "
                  "VALUES (1, ?, ?, ?, ?, 'approved', 'stress', '')", rows)
    for k, v in (("enforce_policy", "1"), ("ssh_enforce", "1"), ("device_cleanup", "0"), ("former_cleanup", "1")):
        set_setting(c, k, v)
    c.commit()
    return {"pcs": n_pcs, "groups": n_groups, "users": n_users, "bookings": len(rows)}


def slot(assign: dict) -> dict:
    c = db()
    for wd, half in slots_now():
        c.execute("DELETE FROM bookings WHERE term_id = 1 AND weekday = ? AND half = ?", (wd, half))
        c.executemany("INSERT INTO bookings(term_id, pc_id, group_id, weekday, half, status, requested_by, "
                      "requested_at) VALUES (1, ?, ?, ?, ?, 'approved', 'stress', '')",
                      [(int(k), int(g), wd, half) for k, g in assign.items()])
    c.commit()
    return {"busy": len(assign), "groups": len(set(assign.values()))}


def report() -> dict:
    r = db().execute("SELECT last_seen, report FROM servers WHERE name = 'hs1'").fetchone()
    rep = json.loads(r[1]) if r and r[1] else {}
    rep.pop("live_policy", None)  # large, and not needed by the runner
    return {"last_seen": r[0] if r else "", **rep}


def stats() -> dict:
    p = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], capture_output=True, text=True)
    out = {}
    for line in p.stdout.splitlines():
        s = json.loads(line)
        used = s["MemUsage"].split("/")[0].strip()
        unit = {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}.get(used[-3:], 0)
        out[s["Name"]] = {"cpu_pct": float(s["CPUPerc"].rstrip("%") or 0), "mem_mb": round(float(used[:-3]) * unit, 1)}
    mem = {l.split(":")[0]: int(l.split()[1]) // 1024 for l in open("/proc/meminfo")}
    return {"containers": out, "load1": float(open("/proc/loadavg").read().split()[0]),
            "mem_available_mb": mem.get("MemAvailable", 0), "mem_total_mb": mem.get("MemTotal", 0)}


def main(args: list[str]) -> object:
    cmd = args[0]
    if cmd == "seed":
        return seed(*map(int, args[1:4]))
    if cmd == "slot":
        return slot(json.loads(args[1]))
    if cmd == "setting":
        c = db(); set_setting(c, args[1], args[2]); c.commit()
        return {args[1]: args[2]}
    if cmd == "force":
        c = db()
        n = int((c.execute("SELECT value FROM settings WHERE key = 'force_seq'").fetchone() or ["0"])[0] or 0) + 1
        set_setting(c, "force_seq", str(n)); c.commit()
        return {"force_seq": n}
    if cmd == "report":
        return report()
    if cmd == "pckeys":
        with ThreadPoolExecutor(8) as pool:
            ks = list(range(1, int(args[1]) + 1))
            return dict(zip(ks, pool.map(lambda k: hs("preauthkeys", "create", "--tags", f"tag:pc-{k}",
                                                      "--reusable", "-e", "48h")["key"], ks)))
    if cmd == "userkeys":
        have = {u["name"]: u["id"] for u in hs("users", "list") or []}

        def key(u: int) -> str:
            name = f"s{u:03d}@stress.test"
            uid = have.get(name) or hs("users", "create", name)["id"]
            return hs("preauthkeys", "create", "--user", str(uid), "--reusable", "-e", "48h")["key"]
        with ThreadPoolExecutor(8) as pool:
            us = list(range(1, int(args[1]) + 1))
            return dict(zip(us, pool.map(key, us)))
    if cmd == "apikey":
        return {"key": (HERE / "secrets" / "agent" / "headscale_api_key").read_text().strip()}
    if cmd == "stats":
        return stats()
    raise SystemExit(__doc__)


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1:]), default=str))
