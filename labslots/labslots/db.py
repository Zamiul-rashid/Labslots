"""SQLite storage. One file, small tables, plain sqlite3."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admins (
  email TEXT PRIMARY KEY, added_at TEXT NOT NULL, added_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS terms (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS groups (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
  linux_account TEXT NOT NULL UNIQUE, color TEXT NOT NULL DEFAULT '#cfe2ff', short TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS members (
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  email TEXT NOT NULL, PRIMARY KEY (group_id, email));
CREATE TABLE IF NOT EXISTS pcs (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, tag TEXT NOT NULL UNIQUE,
  ssh_enabled INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY,
  term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
  pc_id INTEGER NOT NULL REFERENCES pcs(id) ON DELETE CASCADE,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
  half TEXT NOT NULL CHECK (half IN ('day','night')),
  status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected')),
  note TEXT NOT NULL DEFAULT '',
  requested_by TEXT NOT NULL, requested_at TEXT NOT NULL,
  decided_by TEXT, decided_at TEXT);
CREATE INDEX IF NOT EXISTS bookings_cell ON bookings(term_id, pc_id, weekday, half, status);
CREATE TABLE IF NOT EXISTS pc_state (
  pc_id INTEGER PRIMARY KEY REFERENCES pcs(id) ON DELETE CASCADE,
  active_account TEXT, updated_at TEXT, last_error TEXT);
CREATE TABLE IF NOT EXISTS enrolments (
  id INTEGER PRIMARY KEY,
  submitted_by TEXT NOT NULL, submitted_at TEXT NOT NULL,
  group_name TEXT NOT NULL, thesis_title TEXT NOT NULL,
  supervisor TEXT NOT NULL, co_supervisor TEXT NOT NULL DEFAULT '',
  hardware TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','withdrawn')),
  decided_by TEXT, decided_at TEXT, decision_note TEXT NOT NULL DEFAULT '',
  group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS enrolment_members (
  enrolment_id INTEGER NOT NULL REFERENCES enrolments(id) ON DELETE CASCADE,
  position INTEGER NOT NULL, name TEXT NOT NULL, student_id TEXT NOT NULL, email TEXT NOT NULL,
  PRIMARY KEY (enrolment_id, position));
CREATE TABLE IF NOT EXISTS enrolment_supervisors (
  enrolment_id INTEGER NOT NULL REFERENCES enrolments(id) ON DELETE CASCADE,
  position INTEGER NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL,
  PRIMARY KEY (enrolment_id, position));
CREATE TABLE IF NOT EXISTS servers (
  name TEXT PRIMARY KEY, token_sha TEXT NOT NULL, created_at TEXT NOT NULL,
  last_seen TEXT, report TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS former_members (
  email TEXT PRIMARY KEY, since TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
"""

DEFAULT_SETTINGS = {
    # Master switches. Policy enforcement on; destructive steps start in dry-run.
    "enforce_policy": "1",
    "device_cleanup": "0",
    # Devices of students who left every group (group or term removed from the Sheet), after a day's grace.
    "former_cleanup": "1",
    "ssh_enforce": "0",
    "max_devices_per_student": "2",
    "slot_ports": json.dumps(["tcp:22"]),
    "extra_grants": json.dumps([]),
    "extra_ssh": json.dumps([]),
    # The Headscale server a PC belongs to when the Sheet's Server cell is blank.
    "default_server": "hs1",
}


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            self.conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(groups)")}
        if "short" not in cols:  # added after the first deploy
            self.conn.execute("ALTER TABLE groups ADD COLUMN short TEXT NOT NULL DEFAULT ''")
        if "server" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(pcs)")}:
            self.conn.execute("ALTER TABLE pcs ADD COLUMN server TEXT NOT NULL DEFAULT ''")
        for r in self.conn.execute("SELECT id, slug FROM groups WHERE short = ''").fetchall():
            self.conn.execute("UPDATE groups SET short = ? WHERE id = ?", (short_code(r["slug"]), r["id"]))

    # -- helpers ---------------------------------------------------------
    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self.conn.execute(sql, args).lastrowid

    def tx(self):
        """`with db.tx():` runs the block in one transaction."""
        db = self

        class _Tx:
            def __enter__(self):
                db._lock.acquire()
                db.conn.execute("BEGIN IMMEDIATE")

            def __exit__(self, exc_type, *_):
                try:
                    db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    db._lock.release()

        return _Tx()

    # -- settings / audit --------------------------------------------------
    def setting(self, key: str) -> str:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else DEFAULT_SETTINGS.get(key, "")

    def set_setting(self, key: str, value: str) -> None:
        self.x("INSERT INTO settings(key, value) VALUES (?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def flag(self, key: str) -> bool:
        return self.setting(key) == "1"

    def audit(self, actor: str, action: str, detail: str = "") -> None:
        self.x("INSERT INTO audit(ts, actor, action, detail) VALUES (?, ?, ?, ?)",
               (now_iso(), actor, action, detail))

    # -- domain queries ------------------------------------------------------
    def admins(self) -> list[str]:
        return [r["email"] for r in self.q("SELECT email FROM admins ORDER BY email")]

    def is_admin(self, email: str) -> bool:
        return self.one("SELECT 1 FROM admins WHERE email = ?", (email.lower(),)) is not None

    def groups(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM groups ORDER BY name")

    def members(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for r in self.q("SELECT group_id, email FROM members ORDER BY email"):
            out.setdefault(r["group_id"], []).append(r["email"])
        return out

    def pcs(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM pcs ORDER BY name")

    def server_of(self, pc) -> str:
        return pc["server"] or self.setting("default_server")

    def server_names(self) -> list[str]:
        return [r["name"] for r in self.q("SELECT name FROM servers ORDER BY name")]

    def terms(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM terms ORDER BY start_date DESC")

    def terms_on(self, d: date) -> list[sqlite3.Row]:
        iso = d.isoformat()
        return self.q("SELECT * FROM terms WHERE start_date <= ? AND end_date >= ?", (iso, iso))

    def bookings(self, term_id: int, statuses=("pending", "approved")) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.q(
            f"SELECT b.*, g.name AS group_name, g.color AS color, g.slug AS slug, g.short AS short "
            f"FROM bookings b JOIN groups g ON g.id = b.group_id "
            f"WHERE b.term_id = ? AND b.status IN ({marks})", (term_id, *statuses))

    def approved_on(self, d: date, weekday: int, half: str) -> list[sqlite3.Row]:
        """Approved bookings covering one slot (any term whose dates include d)."""
        iso = d.isoformat()
        return self.q(
            "SELECT b.*, g.slug AS slug, g.short AS short, g.name AS group_name, g.color AS color, "
            "g.linux_account AS account, p.tag AS pc_tag, p.name AS pc_name "
            "FROM bookings b JOIN terms t ON t.id = b.term_id "
            "JOIN groups g ON g.id = b.group_id JOIN pcs p ON p.id = b.pc_id "
            "WHERE b.status = 'approved' AND b.weekday = ? AND b.half = ? "
            "AND t.start_date <= ? AND t.end_date >= ?", (weekday, half, iso, iso))


    # -- enrollments -------------------------------------------------------------
    def enrollment(self, eid: int) -> dict | None:
        row = self.one("SELECT e.*, g.name AS live_name FROM enrolments e "
                       "LEFT JOIN groups g ON g.id = e.group_id WHERE e.id = ?", (eid,))
        if not row:
            return None
        e = dict(row)
        # Once enrolled, the group's name is whatever the Sheet now says; sent_name keeps what the form said.
        e["sent_name"] = e["group_name"]
        e["group_name"] = e.pop("live_name") or e["group_name"]
        e["members"] = [dict(m) for m in self.q(
            "SELECT name, student_id, email FROM enrolment_members WHERE enrolment_id = ? ORDER BY position", (eid,))]
        # The first is the supervisor, the rest co-supervisors. Enrollments sent before emails were asked for
        # only have the names, in the supervisor/co_supervisor columns.
        e["supervisors"] = [dict(s) for s in self.q(
            "SELECT name, email FROM enrolment_supervisors WHERE enrolment_id = ? ORDER BY position", (eid,))] or \
            [{"name": n.strip(), "email": ""} for n in [e["supervisor"], *e["co_supervisor"].split(",")] if n.strip()]
        return e

    def enrollments_for(self, email: str) -> list[dict]:
        """Enrollments a person submitted or is listed in, newest first."""
        ids = [r["id"] for r in self.q(
            "SELECT DISTINCT e.id FROM enrolments e LEFT JOIN enrolment_members m ON m.enrolment_id = e.id "
            "WHERE e.submitted_by = ? OR m.email = ? ORDER BY e.id DESC", (email, email))]
        return [self.enrollment(i) for i in ids]


def short_code(slug: str) -> str:
    """Routine-style display code for a group slug: 'nlp' -> 'NLP', 'thesis-a' -> 'TH-A'."""
    slug = slug.upper()
    if len(slug) <= 7:
        return slug
    head, _, rest = slug.partition("-")
    return (f"{head[:2]}-{rest}" if rest else head)[:8]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
