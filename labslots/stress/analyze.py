#!/usr/bin/env python3
"""Turn a stress run's raw data into presentation-ready CSVs.

    python3 analyze.py results/<run> agent-data/agent.db

Reads the run's events/probes/resources/pages/api CSVs plus the slot agent's own audit log (every policy
write, device removal and PC switch, with its time) and writes:
  agent_audit.csv    time, phase, action, detail            (the agent's log for the run's window)
  slot_changes.csv   one row per slot change: how long each step took after the schedule changed
  phases.csv         one row per phase and container: CPU and memory, mean and peak
  summary.csv        phase, metric, value, unit             (the headline numbers)
"""

from __future__ import annotations

import csv
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def rows(path: Path) -> list[dict]:
    return list(csv.DictReader(open(path))) if path.exists() else []


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def main(run: Path, agent_db: Path) -> None:
    events, probes = rows(run / "events.csv"), rows(run / "probes.csv")
    resources, pages, api = rows(run / "resources.csv"), rows(run / "pages.csv"), rows(run / "api.csv")
    start, end = ts(events[0]["time"]), ts(events[-1]["time"])
    phase_starts = [(ts(e["time"]), e["phase"]) for e in events if e["event"] == "phase started"]

    def phase_at(t: datetime) -> str:
        cur = "setup"
        for t0, p in phase_starts:
            if t >= t0:
                cur = p
        return cur

    db = sqlite3.connect(f"file:{agent_db}?mode=ro", uri=True)
    audit = [{"time": r[0], "action": r[1], "detail": r[2]} for r in db.execute(
        "SELECT ts, action, detail FROM audit WHERE ts >= ? AND ts <= ? ORDER BY id",
        (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")))]
    with open(run / "agent_audit.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "phase", "action", "detail"])
        for a in audit:
            w.writerow([a["time"], phase_at(ts(a["time"])), a["action"], a["detail"][:300]])

    summary: list[list] = []
    changes = []
    for e in events:
        if e["event"] != "schedule changed":
            continue
        t0, phase = ts(e["time"]), e["phase"]
        nxt = min([t for t, p in phase_starts if t > t0] or [end])
        window = [a for a in audit if t0 <= ts(a["time"]) < nxt]

        def first(action):
            hits = [ts(a["time"]) for a in window if a["action"] == action]
            return (min(hits) - t0).total_seconds() if hits else None

        def last(action):
            hits = [ts(a["time"]) for a in window if a["action"] == action]
            return (max(hits) - t0).total_seconds() if hits else None

        removed = sum(1 for a in window if a["action"] == "device-removed")
        switched = sum(1 for a in window if a["action"] == "pc-switched")
        errors = sum(1 for a in window if a["action"] == "pc-switch-error")
        policy_s = first("policy-applied")
        # student checks: rounds are rows sharing a t_change value
        rounds: dict[float, list] = defaultdict(list)
        for p in probes:
            if p["phase"] == phase and p["t_change_s"]:
                rounds[float(p["t_change_s"])].append(p)
        correct_s, leaks, denied = None, 0, 0
        for t in sorted(rounds):
            rs = rounds[t]
            ok = all((r["expected"] == "allowed") == (r["result"] == "open") for r in rs)
            if policy_s is not None and t >= policy_s and ok and correct_s is None:
                correct_s = t
            if policy_s is not None and t > policy_s + 20:
                leaks += sum(1 for r in rs if r["expected"] == "blocked" and r["result"] == "open")
            if policy_s is not None and t > policy_s + 60:
                denied += sum(1 for r in rs if r["expected"] == "allowed" and r["result"] != "open")
        relogins = sum(1 for ev in events if ev["phase"] == phase and ev["event"] == "student signed in again")
        back = next((float(ev["value"]) for ev in events if ev["phase"] == phase
                     and ev["event"] == "all incoming students online"), None)
        n_probes = sum(len(v) for v in rounds.values())
        row = {"slot_change": phase.replace("slot change ", ""), "changed_at": e["time"], "detail": e["value"],
               "policy_applied_s": policy_s, "devices_removed": removed,
               "removal_first_s": first("device-removed"), "removal_last_s": last("device-removed"),
               "pcs_switched": switched, "switch_errors": errors, "last_pc_switched_s": last("pc-switched"),
               "students_signed_in_again": relogins, "incoming_students_online_s": back,
               "student_checks": n_probes, "all_checks_correct_s": correct_s,
               "leaks_after_policy": leaks, "wrongly_blocked_after_60s": denied}
        changes.append(row)
        label = f"slot change {row['slot_change']}"
        for metric, key, unit in (("policy written to Headscale", "policy_applied_s", "s"),
                                  ("outgoing devices removed", "devices_removed", "devices"),
                                  ("last device removed", "removal_last_s", "s"),
                                  ("PCs switched over SSH", "pcs_switched", "PCs"),
                                  ("last PC switched", "last_pc_switched_s", "s"),
                                  ("PC switch errors (retried next run)", "switch_errors", "errors"),
                                  ("incoming students all online", "incoming_students_online_s", "s"),
                                  ("every student check correct", "all_checks_correct_s", "s"),
                                  ("student checks run", "student_checks", "checks"),
                                  ("wrong access after the policy (leaks)", "leaks_after_policy", "checks"),
                                  ("wrongly blocked 60 s after the policy", "wrongly_blocked_after_60s", "checks")):
            if row[key] is not None:
                summary.append([label, metric, row[key], unit])
    if changes:
        with open(run / "slot_changes.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(changes[0]))
            w.writeheader()
            w.writerows(changes)

    # resources per phase
    names = {"stress-hs": "Headscale", "stress-hub": "website", "stress-agent": "agent",
             "stress-ts-scheduler": "scheduler sidecar", "pcs": "70 simulated PCs", "students": "simulated students",
             "host-ct": "test container (load avg; MB free)"}
    by = defaultdict(list)
    for r in resources:
        by[(r["phase"], r["container"])].append(r)
    with open(run / "phases.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["phase", "container", "samples", "count_max", "cpu_mean_pct", "cpu_peak_pct", "mem_mean_mb",
                    "mem_peak_mb"])
        for (phase, kind), rs in by.items():
            cpu = [float(r["cpu_pct"]) for r in rs]
            mem = [float(r["mem_mb"]) for r in rs]
            cnt = max((int(r["count"]) for r in rs if r["count"]), default="")
            w.writerow([phase, names.get(kind, kind), len(rs), cnt, round(statistics.mean(cpu), 1), round(max(cpu), 1),
                        round(statistics.mean(mem), 1), round(max(mem), 1)])
            if kind in ("stress-hs", "stress-hub", "stress-agent"):
                summary.append([phase, f"{names[kind]} CPU mean", round(statistics.mean(cpu), 1), "% of one core"])
                summary.append([phase, f"{names[kind]} CPU peak", round(max(cpu), 1), "% of one core"])
                summary.append([phase, f"{names[kind]} memory peak", round(max(mem), 1), "MB"])
    for e in events:
        if e["event"] in ("all PCs online", "all students online", "devices online", "student reconnects"):
            summary.append([e["phase"], e["event"], e["value"], "devices" if e["event"] == "devices online" else
                            ("reconnects" if e["event"] == "student reconnects" else "s")])
    for page in dict.fromkeys(p["page"] for p in pages):
        ms = [int(p["ms"]) for p in pages if p["page"] == page]
        summary.append(["website (70 PCs)", f"page {page}, median load", statistics.median(ms), "ms"])
    if api:
        ms = sorted(int(r["ms"]) for r in api)
        summary.append(["all", "Headscale API list nodes, median", statistics.median(ms), "ms"])
        summary.append(["all", "Headscale API list nodes, 95th percentile", ms[int(len(ms) * 0.95) - 1], "ms"])
        summary.append(["all", "devices online, peak", max(int(r["online"]) for r in api), "devices"])
    with open(run / "summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["phase", "metric", "value", "unit"])
        w.writerows(summary)
    print(f"wrote agent_audit.csv, slot_changes.csv, phases.csv, summary.csv in {run}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
