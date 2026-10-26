"""Hub side of the hub/agent split: what each Headscale server's slot agent is sent, and what it reports.

The website (hub) holds the schedule (from the Google Sheet). Each slot agent asks
for its server's slice: the PCs on that server, the groups booked on them, their
bookings, the admins and the enforcement settings. The agent stores the slice in
its own SQLite file with the same schema (apply_slice), so the existing Enforcer
runs unchanged there, and it keeps enforcing that copy whenever the hub is down.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta

from .db import DB, now_iso

SERVER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
SLICE_SETTINGS = ("enforce_policy", "device_cleanup", "former_cleanup", "ssh_enforce", "max_devices_per_student",
                  "slot_ports", "extra_grants", "extra_ssh")
STALE_AFTER = timedelta(minutes=2)


def token_sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def add_server(db: DB, name: str) -> str:
    """Register (or re-key) a server; returns its new token, shown once."""
    token = secrets.token_urlsafe(32)
    db.x("INSERT INTO servers(name, token_sha, created_at) VALUES (?, ?, ?) "
         "ON CONFLICT(name) DO UPDATE SET token_sha = excluded.token_sha", (name, token_sha(token), now_iso()))
    return token


def server_for_token(db: DB, token: str) -> str | None:
    if not token:
        return None
    row = db.one("SELECT name FROM servers WHERE token_sha = ?", (token_sha(token),))
    return row["name"] if row else None


def server_slice(db: DB, server: str) -> dict:
    pcs = [dict(p) for p in db.pcs() if db.server_of(p) == server]
    pc_ids = {p["id"] for p in pcs}
    marks = ",".join("?" * len(pc_ids)) or "NULL"
    bookings = [dict(b) for b in db.q(
        f"SELECT id, term_id, pc_id, group_id, weekday, half FROM bookings "
        f"WHERE status = 'approved' AND pc_id IN ({marks})", tuple(pc_ids))]
    rot_raw, rot_groups = db.setting("test_rotation"), []
    try:
        rot = json.loads(rot_raw) if rot_raw else None
    except ValueError:
        rot = None
    if rot and rot.get("pc_id") in pc_ids:
        rot_groups = rot.get("groups", [])
    else:
        rot_raw = ""
    gids = {b["group_id"] for b in bookings} | set(rot_groups)
    members = db.members()
    groups = [dict(g) | {"members": members.get(g["id"], [])} for g in db.groups() if g["id"] in gids]
    body = {
        "server": server,
        "admins": db.admins(),
        "terms": [dict(t) for t in db.terms()],
        "pcs": pcs,
        "groups": groups,
        "bookings": bookings,
        "settings": {k: db.setting(k) for k in SLICE_SETTINGS} | {"test_rotation": rot_raw},
        "force": db.setting("force_seq") or "0",
    }
    body["version"] = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return body


def apply_slice(db: DB, s: dict, now: datetime | None = None) -> None:
    """Make an agent's SQLite match a slice. Keeps pc_state (what each PC runs) for PCs that stay.

    Students who drop off this server's roster (their group, its term or all its slots were removed
    from the Sheet) are remembered in former_members, so the Enforcer can remove their devices."""
    since = (now or datetime.now()).isoformat(timespec="seconds")
    with db.tx():
        c = db.conn
        before = {r["email"] for r in c.execute("SELECT email FROM members")}
        for k, v in s["settings"].items():
            c.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))
        c.execute("DELETE FROM admins")
        c.executemany("INSERT INTO admins(email, added_at, added_by) VALUES (?, '', 'hub')",
                      [(a,) for a in s["admins"]])
        c.execute("DELETE FROM bookings")
        c.execute("DELETE FROM members")
        c.execute("DELETE FROM groups")
        c.execute("DELETE FROM terms")
        c.executemany("INSERT INTO terms(id, name, start_date, end_date) VALUES (?, ?, ?, ?)",
                      [(t["id"], t["name"], t["start_date"], t["end_date"]) for t in s["terms"]])
        keep = {p["id"] for p in s["pcs"]}
        for r in c.execute("SELECT id FROM pcs").fetchall():
            if r["id"] not in keep:
                c.execute("DELETE FROM pcs WHERE id = ?", (r["id"],))
        c.execute("UPDATE pcs SET tag = 'tag:-moving-' || id")  # lets tags move between ids without a clash
        for p in s["pcs"]:
            c.execute("INSERT INTO pcs(id, name, tag, ssh_enabled, notes, server) VALUES (?, ?, ?, ?, ?, ?) "
                      "ON CONFLICT(id) DO UPDATE SET name = excluded.name, tag = excluded.tag, "
                      "ssh_enabled = excluded.ssh_enabled, notes = excluded.notes, server = excluded.server",
                      (p["id"], p["name"], p["tag"], p["ssh_enabled"], p.get("notes", ""), p.get("server", "")))
        for g in s["groups"]:
            c.execute("INSERT INTO groups(id, name, slug, linux_account, color, short, notes) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (g["id"], g["name"], g["slug"], g["linux_account"], g["color"], g.get("short", ""),
                       g.get("notes", "")))
            c.executemany("INSERT INTO members(group_id, email) VALUES (?, ?)", [(g["id"], m) for m in g["members"]])
        c.executemany("INSERT INTO bookings(id, term_id, pc_id, group_id, weekday, half, status, requested_by, "
                      "requested_at) VALUES (?, ?, ?, ?, ?, ?, 'approved', 'hub', '')",
                      [(b["id"], b["term_id"], b["pc_id"], b["group_id"], b["weekday"], b["half"])
                       for b in s["bookings"]])
        stay = {m.lower() for g in s["groups"] for m in g["members"]} | {a.lower() for a in s["admins"]}
        c.executemany("INSERT OR IGNORE INTO former_members(email, since) VALUES (?, ?)",
                      [(e, since) for e in sorted({e.lower() for e in before} - stay)])
        c.executemany("DELETE FROM former_members WHERE email = ?", [(e,) for e in stay])


def store_report(db: DB, server: str, report: dict) -> None:
    db.x("UPDATE servers SET last_seen = ?, report = ? WHERE name = ?",
         (now_iso(), json.dumps(report, default=str)[:400_000], server))


def reports(db: DB, now: datetime | None = None) -> list[dict]:
    """Every registered server with its latest report and whether it has gone quiet."""
    now = now or datetime.now()
    out = []
    for r in db.q("SELECT * FROM servers ORDER BY name"):
        try:
            rep = json.loads(r["report"]) if r["report"] else {}
        except ValueError:
            rep = {}
        seen = datetime.fromisoformat(r["last_seen"]) if r["last_seen"] else None
        out.append({"name": r["name"], "last_seen": r["last_seen"] or "", "report": rep,
                    "stale": seen is None or now - seen > STALE_AFTER})
    return out


def node_index(reps: list[dict]) -> dict[str, dict]:
    """Tag -> node as last reported by the agents (PC tailnet state)."""
    out = {}
    for r in reps:
        for n in r["report"].get("nodes", []):
            for t in n.get("tags", []):
                out.setdefault(t, n | {"server": r["name"]})
    return out


def pc_states(reps: list[dict]) -> dict[int, dict]:
    out = {}
    for r in reps:
        for pid, st in r["report"].get("pc_state", {}).items():
            out[int(pid)] = st
    return out
