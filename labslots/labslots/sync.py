"""Google Sheet -> SQLite sync. The Sheet is the source of truth; SQLite keeps the last good copy.

Every pull reads the whole Sheet in one request, checks every row and cell, and
writes the result into the SQLite tables the website and enforcer already use.
A bad cell never breaks the rest: it keeps its last good value (or is left out
when it never had one) and is listed in the Problems tab. A tab whose header row
is missing or changed is ignored as a whole, so deleting or mangling a tab cannot
wipe the schedule. If Google cannot be reached, nothing changes and enforcement
carries on from SQLite.

The Sheet is built from SQLite on the first run (or on "Restore Sheet from
backup"): one tab each for Groups, PCs, Terms and Admins, one Routine tab per
term, plus Problems and Enrollments, which only the sync writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .db import DB, now_iso, short_code
from .hub import SERVER_RE
from .policy import SLUG_RE, TAG_RE
from .sheet_style import style_requests
from .sheets import SheetsError, a1, quote
from .slots import SWITCH_WINDOW, span

log = logging.getLogger("labslots.sync")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ACCOUNT_RE = re.compile(r"^grp-[a-z0-9-]{1,28}$")
SHORT_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,7}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SLOT_COLS = [(wd, half) for wd in range(7) for half in ("day", "night")]
DEFAULT_COLOR = "#cfe2ff"

README, GROUPS, PCS, TERMS, ADMINS, PROBLEMS, ENROLLMENTS = (
    "Read me", "Groups", "PCs", "Terms", "Admins", "Problems", "Enrollments")
ROUTINE = "Routine "
OLD_ENROLLMENTS = "Enrolments"  # the tab's earlier spelling; renamed on the next read
HEADERS = {
    GROUPS: ["Code", "Name", "Short", "Colour", "Members (one email per line)", "Notes"],
    PCS: ["Name", "Tag", "Account switching", "Notes", "Server"],
    TERMS: ["Name", "Starts", "Ends"],
    ADMINS: ["Email"],
    PROBLEMS: ["Where", "Value", "Problem", "Enforcing instead", "Since"],
    ENROLLMENTS: ["#", "Sent", "Status", "Group", "Thesis", "Supervisor", "Co-supervisor", "Members",
                 "Hardware needs", "Decided by", "Note"],
}
ROUTINE_HEADER = ["PC"] + [f"{WEEKDAYS[wd]} {half}\n{span(half)}" for wd, half in SLOT_COLS]
# Highlighter colours for groups, in an order where neighbours differ in hue (yellow, green, pink, blue, ...).
PALETTE = ("#ffe08a", "#b8e6b0", "#f7c6d9", "#bcdcf5", "#ffd0a6", "#d9ccf2", "#c9e7e1", "#f4d6b8",
           "#e9f0a6", "#a8dcd1", "#f2b8b0", "#c6d4f7", "#f7e3b0", "#d6eac3", "#e8c9ef", "#b9e3ef")


def next_color(used: list[str]) -> str:
    """The first palette colour no group uses yet, else the least used one."""
    counts = {c: 0 for c in PALETTE}
    for c in used:
        if c.lower() in counts:
            counts[c.lower()] += 1
    return min(PALETTE, key=lambda c: (counts[c], PALETTE.index(c)))
README_TEXT = [
    ["PC slots"],
    [""],
    ["This Sheet is the lab's schedule. The slot system reads it every 30 seconds and enforces it:"],
    ["only the group in a slot can reach its PC over the lab network."],
    [""],
    ["Routine tabs (one per term): one row per PC, one column per slot. Pick a group's short code in a cell."],
    [f"  day = {span('day', '-')}, night = {span('night', '-')} the next morning (local time). A blank cell is a free slot."],
    [f"  Each slot ends {SWITCH_WINDOW.seconds // 60} minutes before the next starts: the outgoing group is shut out first."],
    ["Groups: one row per thesis group. Code becomes group:<code> and the Linux account grp-<code>."],
    ["  Short is what the Routine tabs show (blank = made from the code); Colour is its highlighter colour."],
    ["  Members: one student email per line (Alt+Enter for a new line in a cell)."],
    ["PCs: Tag must match the PC's Headscale tag, e.g. tag:pc-41. Account switching: Yes once the PC is prepared."],
    ["  Server: the Headscale server the PC is on, as registered on the website (blank = the default server)."],
    ["Terms: the date range each Routine tab repeats in. Add a term here and its Routine tab appears."],
    ["Admins: who can use the admin website and reach every PC."],
    [""],
    ["Problems is written by the system: anything it could not use, and what it enforces instead."],
    ["Enrollments is a copy of the website's enrollment queue. Edits there are overwritten."],
    ["Do not rename tabs or change header rows; a tab with a changed header is ignored until it is fixed."],
]


def routine_tab(term_name: str) -> str:
    return ROUTINE + term_name


# -- cell parsing --------------------------------------------------------------------------
def text(row: list, i: int) -> str:
    if i >= len(row) or row[i] is None:
        return ""
    v = row[i]
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def parse_date(v) -> date | None:
    if isinstance(v, bool) or v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return date(1899, 12, 30) + timedelta(days=int(v))  # Sheets serial date
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_bool(v: str) -> bool | None:
    s = v.strip().lower()
    if s in ("yes", "y", "true", "on", "1"):
        return True
    if s in ("no", "n", "false", "off", "0", ""):
        return False
    return None


def header_ok(rows: list[list], expected: list[str]) -> bool:
    if not rows:
        return False
    return all(text(rows[0], i).lower().split(" ")[0] == h.lower().split(" ")[0] for i, h in enumerate(expected))


STYLE_VERSION = 2  # bump to restyle every Sheet after changing sheet_style.py


def routine_header_ok(rows: list[list]) -> bool:
    """Day columns are compared on their first line ("Mon day"), so older one-line headers still count."""
    return bool(rows) and [text(rows[0], i).split("\n")[0].strip().lower() for i in range(15)] == \
        [h.split("\n")[0].lower() for h in ROUTINE_HEADER]


def mass_removal(gone: int, before: int) -> bool:
    """More than 3 rows, and more than half the tab, vanishing in one edit: hold it."""
    return gone > 3 and gone * 2 > before


def blank(row: list) -> bool:
    return not any(text(row, i) for i in range(len(row)))


@dataclass
class Problem:
    where: str
    value: str
    problem: str
    instead: str

    def key(self) -> str:
        return f"{self.where}|{self.value}|{self.problem}"


@dataclass
class Snapshot:
    terms: dict | None = None      # name -> (start, end)
    pcs: dict | None = None        # tag -> {name, ssh, notes}
    groups: dict | None = None     # slug -> {name, short, color, notes, members}
    admins: list | None = None
    routine: dict = field(default_factory=dict)   # term name -> {(tag, wd, half): slug}
    problems: list = field(default_factory=list)
    lookup: dict = field(default_factory=dict)    # cell text (lower) -> group code: codes, short codes, old short codes
    group_rows: dict = field(default_factory=dict)  # code -> Groups row number, and whether Short/Colour were blank


@dataclass
class SyncReport:
    ok: bool = False
    error: str = ""
    seeded: bool = False
    changes: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    at: str = ""


class AssignError(Exception):
    pass


class Sync:
    def __init__(self, db: DB, sheet, bootstrap_admins: list[str] | None = None):
        self.db = db
        self.sheet = sheet
        self.bootstrap = [a.lower() for a in (bootstrap_admins or [])]
        self.lock = threading.Lock()
        self.last: SyncReport | None = None

    @property
    def url(self) -> str:
        return self.sheet.url

    # -- status ---------------------------------------------------------------------------
    def status(self) -> dict:
        return {"url": self.url, "last_ok": self.db.setting("sheet_last_ok"),
                "error": self.db.setting("sheet_error"), "error_since": self.db.setting("sheet_error_since"),
                "problems": int(self.db.setting("sheet_problem_count") or 0), "email": self.sheet.email}

    def _fail(self, rep: SyncReport, msg: str) -> SyncReport:
        rep.error = msg
        if not self.db.setting("sheet_error"):
            self.db.set_setting("sheet_error_since", now_iso())
            self.db.audit("sheet", "sheet-unreachable", msg[:500])
        self.db.set_setting("sheet_error", msg[:500])
        self.last = rep
        return rep

    # -- pull -----------------------------------------------------------------------------
    def pull(self) -> SyncReport:
        with self.lock:
            return self._pull()

    def _pull(self) -> SyncReport:
        rep = SyncReport(at=now_iso())
        try:
            ids = self.sheet.tab_ids()
            if OLD_ENROLLMENTS in ids and ENROLLMENTS not in ids:
                self.sheet.batch_update([{"updateSheetProperties": {
                    "properties": {"sheetId": ids[OLD_ENROLLMENTS], "title": ENROLLMENTS}, "fields": "title"}}])
                ids = self.sheet.tab_ids()
            if GROUPS not in ids:
                self._build(ids, restore=False)
                rep.seeded = True
                ids = self.sheet.tab_ids()
            wanted = [t for t in ids if t in HEADERS or t.startswith(ROUTINE) or t == README]
            data = self.sheet.read(wanted)
        except Exception as e:  # noqa: BLE001
            return self._fail(rep, f"{e}")
        snap = self.parse(data)
        try:
            rep.changes = self.apply(snap)
        except Exception as e:  # noqa: BLE001
            log.exception("applying sheet")
            return self._fail(rep, f"saving the Sheet's data: {e}")
        if rep.changes:
            self.db.audit("sheet", "sheet-sync", "; ".join(rep.changes)[:1500])
        try:
            self._tidy(ids, data, snap)
        except Exception as e:  # noqa: BLE001
            return self._fail(rep, f"updating the Sheet: {e}")
        if self.db.setting("sheet_error"):
            self.db.audit("sheet", "sheet-reachable", "reading the Sheet again")
        self.db.set_setting("sheet_error", "")
        self.db.set_setting("sheet_error_since", "")
        self.db.set_setting("sheet_last_ok", rep.at)
        self.db.set_setting("sheet_problem_count", str(len(snap.problems)))
        rep.ok, rep.problems = True, snap.problems
        self.last = rep
        return rep

    # -- parse ----------------------------------------------------------------------------
    def parse(self, data: dict[str, list[list]]) -> Snapshot:
        snap = Snapshot()
        P = snap.problems

        def bad_tab(tab):
            P.append(Problem(tab, "", "The tab is missing or its header row was changed",
                             f"Kept every {tab.lower()} entry as it was. Restore the header: "
                             + ", ".join(HEADERS.get(tab, ROUTINE_HEADER[:3]))))

        old_terms = {r["name"]: r for r in self.db.q("SELECT * FROM terms")}
        old_pcs = {r["tag"]: r for r in self.db.q("SELECT * FROM pcs")}
        old_groups = {r["slug"]: r for r in self.db.q("SELECT * FROM groups")}

        # Terms
        rows = data.get(TERMS)
        if header_ok(rows or [], HEADERS[TERMS]):
            snap.terms = {}
            for n, row in enumerate(rows[1:], start=2):
                if blank(row):
                    continue
                where = f"{TERMS} row {n}"
                name = text(row, 0)
                if not name:
                    P.append(Problem(where, " · ".join(text(row, i) for i in (1, 2)), "No term name", "Row ignored"))
                    continue
                if name in snap.terms:
                    P.append(Problem(where, name, "Another row already has this term name", "Row ignored"))
                    continue
                s, e = parse_date(row[1] if len(row) > 1 else ""), parse_date(row[2] if len(row) > 2 else "")
                if s and e and s <= e:
                    snap.terms[name] = (s, e)
                elif name in old_terms:
                    o = old_terms[name]
                    snap.terms[name] = (date.fromisoformat(o["start_date"]), date.fromisoformat(o["end_date"]))
                    P.append(Problem(where, f"{text(row, 1)} to {text(row, 2)}", "Dates missing, unreadable or in the wrong order",
                                     f"Kept {o['start_date']} to {o['end_date']}"))
                else:
                    P.append(Problem(where, f"{name}: {text(row, 1)} to {text(row, 2)}",
                                     "Dates missing, unreadable or in the wrong order (use YYYY-MM-DD)", "Term not used"))
        else:
            bad_tab(TERMS)

        # PCs (keyed by Headscale tag)
        rows = data.get(PCS)
        if header_ok(rows or [], HEADERS[PCS][:4]):  # Server was added later; _tidy writes its header
            snap.pcs = {}
            by_name = {r["name"].lower(): r for r in old_pcs.values()}
            default = self.db.setting("default_server")
            servers = set(self.db.server_names()) | {default}
            for n, row in enumerate(rows[1:], start=2):
                if blank(row):
                    continue
                where = f"{PCS} row {n}"
                name, tag = text(row, 0), text(row, 1).lower()
                if tag and not tag.startswith("tag:"):
                    tag = "tag:" + tag
                if not TAG_RE.match(tag):
                    old = by_name.get(name.lower())
                    if old and old["tag"] not in snap.pcs:
                        snap.pcs[old["tag"]] = {"name": old["name"], "ssh": bool(old["ssh_enabled"]), "notes": old["notes"],
                                                "server": old["server"] or default}
                        P.append(Problem(where, text(row, 1), "Tag must look like tag:pc-41", f"Kept {old['tag']}"))
                    else:
                        P.append(Problem(where, text(row, 1) or "(blank)", "Tag must look like tag:pc-41", "PC not used"))
                    continue
                if tag in snap.pcs:
                    P.append(Problem(where, tag, "Another row already has this tag", "Row ignored"))
                    continue
                old = old_pcs.get(tag)
                ssh = parse_bool(text(row, 2))
                if ssh is None:
                    ssh = bool(old["ssh_enabled"]) if old else False
                    P.append(Problem(where, text(row, 2), "Account switching must be Yes or No",
                                     f"Kept {'Yes' if ssh else 'No'}"))
                server = text(row, 4).lower() or default
                if not SERVER_RE.match(server) or server not in servers:
                    keep = (old["server"] or default) if old else default
                    P.append(Problem(where, text(row, 4), "Not a Headscale server registered on the website "
                                     "(Settings, Headscale servers)", f"Kept {keep}"))
                    server = keep
                snap.pcs[tag] = {"name": name or (old["name"] if old else tag[4:]), "ssh": ssh, "notes": text(row, 3),
                                 "server": server}
            gone = old_pcs.keys() - snap.pcs.keys()
            if mass_removal(len(gone), len(old_pcs)):
                for tag in sorted(gone):
                    o = old_pcs[tag]
                    snap.pcs[tag] = {"name": o["name"], "ssh": bool(o["ssh_enabled"]), "notes": o["notes"],
                                     "server": o["server"] or default}
                P.append(Problem(PCS, f"{len(gone)} PCs", "Removing this many PCs at once looks like an accident",
                                 "Kept them. Remove PCs a few at a time, or restore the rows"))
        else:
            bad_tab(PCS)

        # Groups (keyed by code)
        rows = data.get(GROUPS)
        if header_ok(rows or [], HEADERS[GROUPS]):
            snap.groups = {}
            shorts: dict[str, str] = {}
            for n, row in enumerate(rows[1:], start=2):
                if blank(row):
                    continue
                where = f"{GROUPS} row {n}"
                code = text(row, 0).lower()
                if not SLUG_RE.match(code) or not ACCOUNT_RE.match(f"grp-{code}"):
                    P.append(Problem(where, text(row, 0) or "(blank)",
                                     "Code must be lowercase letters, digits and dashes, up to 28 characters",
                                     "Group not used"))
                    continue
                if code in snap.groups:
                    P.append(Problem(where, code, "Another row already has this code", "Row ignored"))
                    continue
                old = old_groups.get(code)
                g = {"name": text(row, 1) or (old["name"] if old else code), "notes": text(row, 5), "members": []}
                short = text(row, 2).upper() or (old["short"] if old else "") or short_code(code)
                if not SHORT_RE.match(short):
                    keep = old["short"] if old else short_code(code)
                    P.append(Problem(where, text(row, 2), "Short code: up to 8 capital letters, digits or dashes", f"Kept {keep}"))
                    short = keep
                if short in shorts:
                    P.append(Problem(where, short, f"Short code already used by {shorts[short]}", f"Using {code.upper()[:8]}"))
                    short = code.upper()[:8]
                shorts[short] = code
                g["short"] = short
                color = text(row, 3) or (old["color"] if old else
                                         next_color([g_["color"] for g_ in snap.groups.values()]))
                if not COLOR_RE.match(color):
                    keep = old["color"] if old else DEFAULT_COLOR
                    P.append(Problem(where, text(row, 3), "Colour must look like #ffe08a", f"Kept {keep}"))
                    color = keep
                g["color"] = color
                seen = set()
                for m in re.split(r"[\s,;]+", text(row, 4)):
                    m = m.strip().lower()
                    if not m or m in seen:
                        continue
                    if EMAIL_RE.match(m):
                        seen.add(m)
                        g["members"].append(m)
                    else:
                        P.append(Problem(where, m, "Not an email address", "Left out of the group"))
                snap.groups[code] = g
                snap.group_rows[code] = (n, not text(row, 2), not text(row, 3))
            gone = old_groups.keys() - snap.groups.keys()
            if mass_removal(len(gone), len(old_groups)):
                members = self.db.members()
                for slug in sorted(gone):
                    o = old_groups[slug]
                    snap.groups[slug] = {"name": o["name"], "short": o["short"], "color": o["color"],
                                         "notes": o["notes"], "members": members.get(o["id"], [])}
                P.append(Problem(GROUPS, f"{len(gone)} groups", "Removing this many groups at once looks like an accident",
                                 "Kept them. Remove groups a few at a time, or restore the rows"))
        else:
            bad_tab(GROUPS)

        # Admins
        rows = data.get(ADMINS)
        if header_ok(rows or [], HEADERS[ADMINS]):
            admins = []
            for n, row in enumerate(rows[1:], start=2):
                e = text(row, 0).lower()
                if not e:
                    continue
                if EMAIL_RE.match(e):
                    if e not in admins:
                        admins.append(e)
                else:
                    P.append(Problem(f"{ADMINS} row {n}", e, "Not an email address", "Row ignored"))
            if admins:
                snap.admins = sorted(set(admins) | set(self.bootstrap))
            else:
                P.append(Problem(ADMINS, "", "No valid admin emails", "Kept the current admins"))
        else:
            bad_tab(ADMINS)

        # Routine tabs, against the effective PCs and groups (the old ones when a tab was ignored)
        pcs = snap.pcs if snap.pcs is not None else {t: {"name": r["name"]} for t, r in old_pcs.items()}
        groups = snap.groups if snap.groups is not None else {
            s: {"short": r["short"]} for s, r in old_groups.items()}
        terms = snap.terms if snap.terms is not None else {n: None for n in old_terms}
        pc_lookup = {}
        for tag, p in pcs.items():
            pc_lookup[p["name"].lower()] = tag
            pc_lookup[tag] = tag
            pc_lookup[tag[4:]] = tag
        code_lookup = {r["short"].lower(): s for s, r in old_groups.items() if r["short"] and s in groups}
        code_lookup.update({s: s for s in groups})
        code_lookup.update({g["short"].lower(): s for s, g in groups.items() if g.get("short")})
        snap.lookup = code_lookup
        for term in terms:
            tab = routine_tab(term)
            if tab not in data:
                continue  # created by _tidy; the term's bookings stay as they are until then
            rows = data[tab]
            if not routine_header_ok(rows):
                bad_tab(tab)
                continue
            old = self._old_routine(term)
            cells: dict = {}
            seen_pcs = set()
            for n, row in enumerate(rows[1:], start=2):
                if blank(row):
                    continue
                label = text(row, 0)
                tag = pc_lookup.get(label.lower())
                if not tag:
                    P.append(Problem(f"{tab} row {n}", label or "(blank)", "Not a PC in the PCs tab", "Row ignored"))
                    continue
                if tag in seen_pcs:
                    P.append(Problem(f"{tab} row {n}", label, "This PC already has a row above", "Row ignored"))
                    continue
                seen_pcs.add(tag)
                for k, (wd, half) in enumerate(SLOT_COLS):
                    v = text(row, k + 1)
                    if not v:
                        continue
                    slug = code_lookup.get(v.lower())
                    if slug:
                        cells[(tag, wd, half)] = slug
                        continue
                    was = old.get((tag, wd, half))
                    where = f"{tab} · {label} · {WEEKDAYS[wd]} {half}"
                    if was and was in groups:
                        cells[(tag, wd, half)] = was
                        P.append(Problem(where, v, "Not a group code from the Groups tab", f"Kept {was}"))
                    else:
                        P.append(Problem(where, v, "Not a group code from the Groups tab", "Slot left free"))
            snap.routine[term] = cells
        return snap

    def _old_routine(self, term: str) -> dict:
        return {(r["tag"], r["weekday"], r["half"]): r["slug"] for r in self.db.q(
            "SELECT p.tag, b.weekday, b.half, g.slug FROM bookings b JOIN terms t ON t.id = b.term_id "
            "JOIN pcs p ON p.id = b.pc_id JOIN groups g ON g.id = b.group_id "
            "WHERE t.name = ? AND b.status = 'approved'", (term,))}

    # -- apply ----------------------------------------------------------------------------
    def apply(self, snap: Snapshot) -> list[str]:
        ch: list[str] = []
        db = self.db
        with db.tx():
            c = db.conn
            if snap.terms is not None:
                old = {r["name"]: r for r in c.execute("SELECT * FROM terms")}
                for name, (s, e) in snap.terms.items():
                    if name not in old:
                        c.execute("INSERT INTO terms(name, start_date, end_date) VALUES (?, ?, ?)",
                                  (name, s.isoformat(), e.isoformat()))
                        ch.append(f"added term {name}")
                    elif (old[name]["start_date"], old[name]["end_date"]) != (s.isoformat(), e.isoformat()):
                        c.execute("UPDATE terms SET start_date = ?, end_date = ? WHERE name = ?",
                                  (s.isoformat(), e.isoformat(), name))
                        ch.append(f"term {name} now {s} to {e}")
                for name in old.keys() - snap.terms.keys():
                    c.execute("DELETE FROM terms WHERE name = ?", (name,))
                    ch.append(f"removed term {name}")
            if snap.pcs is not None:
                old = {r["tag"]: r for r in c.execute("SELECT * FROM pcs")}
                for tag, p in snap.pcs.items():
                    o = old.get(tag)
                    if not o:
                        c.execute("INSERT INTO pcs(name, tag, ssh_enabled, notes, server) VALUES (?, ?, ?, ?, ?)",
                                  (p["name"], tag, int(p["ssh"]), p["notes"], p["server"]))
                        ch.append(f"added PC {p['name']}")
                    elif (o["name"], bool(o["ssh_enabled"]), o["notes"], db.server_of(o)) != \
                            (p["name"], p["ssh"], p["notes"], p["server"]):
                        c.execute("UPDATE pcs SET name = ?, ssh_enabled = ?, notes = ?, server = ? WHERE tag = ?",
                                  (p["name"], int(p["ssh"]), p["notes"], p["server"], tag))
                        if db.server_of(o) != p["server"]:
                            ch.append(f"{p['name']} moved to server {p['server']}")
                        elif bool(o["ssh_enabled"]) != p["ssh"]:
                            c.execute("DELETE FROM pc_state WHERE pc_id = ?", (o["id"],))
                            ch.append(f"{p['name']} account switching {'on' if p['ssh'] else 'off'}")
                        else:
                            ch.append(f"updated PC {p['name']}")
                for tag in old.keys() - snap.pcs.keys():
                    c.execute("DELETE FROM pcs WHERE tag = ?", (tag,))
                    ch.append(f"removed PC {old[tag]['name']}")
            if snap.groups is not None:
                old = {r["slug"]: r for r in c.execute("SELECT * FROM groups")}
                members: dict[int, list[str]] = {}
                for r in c.execute("SELECT group_id, email FROM members ORDER BY email"):
                    members.setdefault(r["group_id"], []).append(r["email"])
                for slug, g in snap.groups.items():
                    o = old.get(slug)
                    if not o:
                        gid = c.execute("INSERT INTO groups(name, slug, linux_account, color, short, notes) "
                                        "VALUES (?, ?, ?, ?, ?, ?)", (g["name"], slug, f"grp-{slug}", g["color"],
                                                                      g["short"], g["notes"])).lastrowid
                        ch.append(f"added group {slug}")
                    else:
                        gid = o["id"]
                        if (o["name"], o["color"], o["short"], o["notes"]) != (g["name"], g["color"], g["short"], g["notes"]):
                            c.execute("UPDATE groups SET name = ?, color = ?, short = ?, notes = ? WHERE id = ?",
                                      (g["name"], g["color"], g["short"], g["notes"], gid))
                            ch.append(f"updated group {slug}")
                    if sorted(members.get(gid, [])) != sorted(g["members"]):
                        c.execute("DELETE FROM members WHERE group_id = ?", (gid,))
                        c.executemany("INSERT INTO members(group_id, email) VALUES (?, ?)",
                                      [(gid, m) for m in g["members"]])
                        if o:
                            ch.append(f"{slug} members now {len(g['members'])}")
                for slug in old.keys() - snap.groups.keys():
                    c.execute("DELETE FROM groups WHERE slug = ?", (slug,))
                    ch.append(f"removed group {slug}")
            if snap.admins is not None:
                old = {r["email"] for r in c.execute("SELECT email FROM admins")}
                for e in sorted(set(snap.admins) - old):
                    c.execute("INSERT INTO admins(email, added_at, added_by) VALUES (?, ?, 'sheet')", (e, now_iso()))
                    ch.append(f"added admin {e}")
                for e in sorted(old - set(snap.admins)):
                    c.execute("DELETE FROM admins WHERE email = ?", (e,))
                    ch.append(f"removed admin {e}")
            term_ids = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM terms")}
            pc_ids = {r["tag"]: (r["id"], r["name"]) for r in c.execute("SELECT id, tag, name FROM pcs")}
            group_ids = {r["slug"]: r["id"] for r in c.execute("SELECT id, slug FROM groups")}
            for term, cells in snap.routine.items():
                tid = term_ids.get(term)
                if not tid:
                    continue
                want = {(pc_ids[t][0], wd, h): group_ids[s] for (t, wd, h), s in cells.items()
                        if t in pc_ids and s in group_ids}
                have = {(r["pc_id"], r["weekday"], r["half"]): (r["id"], r["group_id"]) for r in c.execute(
                    "SELECT id, pc_id, weekday, half, group_id FROM bookings WHERE term_id = ? AND status = 'approved'",
                    (tid,))}
                names = {v[0]: v[1] for v in pc_ids.values()}
                slugs = {v: k for k, v in group_ids.items()}
                for key, gid in want.items():
                    label = f"{names[key[0]]} {WEEKDAYS[key[1]]} {key[2]}"
                    if key not in have:
                        c.execute("INSERT INTO bookings(term_id, pc_id, group_id, weekday, half, status, note, "
                                  "requested_by, requested_at, decided_by, decided_at) "
                                  "VALUES (?, ?, ?, ?, ?, 'approved', 'from the Sheet', 'sheet', ?, 'sheet', ?)",
                                  (tid, key[0], gid, key[1], key[2], now_iso(), now_iso()))
                        ch.append(f"{term}: {label} = {slugs[gid]}")
                    elif have[key][1] != gid:
                        c.execute("UPDATE bookings SET group_id = ?, decided_by = 'sheet', decided_at = ? WHERE id = ?",
                                  (gid, now_iso(), have[key][0]))
                        ch.append(f"{term}: {label} = {slugs[gid]}")
                for key in have.keys() - want.keys():
                    c.execute("DELETE FROM bookings WHERE id = ?", (have[key][0],))
                    ch.append(f"{term}: {names.get(key[0], '?')} {WEEKDAYS[key[1]]} {key[2]} freed")
        return ch

    # -- keep the Sheet tidy ------------------------------------------------------------------
    def _tidy(self, ids: dict, data: dict, snap: Snapshot) -> None:
        """Routine tab per term, a row per PC, the Problems list and the Enrollments copy."""
        pcs = self.db.pcs()
        shorts = {g["slug"]: g["short"] or short_code(g["slug"]) for g in self.db.groups()}
        fixes: dict = {}
        for code, (n, no_short, no_color) in snap.group_rows.items():
            g = self.db.one("SELECT short, color FROM groups WHERE slug = ?", (code,))
            if g and no_short:
                fixes[a1(GROUPS, n - 1, 2)] = [[g["short"]]]
            if g and no_color:
                fixes[a1(GROUPS, n - 1, 3)] = [[g["color"]]]
        for tab, rows in data.items():
            if not tab.startswith(ROUTINE) or not routine_header_ok(rows):
                continue
            if [text(rows[0], i) for i in range(15)] != ROUTINE_HEADER:
                fixes[a1(tab, 0, 0)] = [ROUTINE_HEADER]
            for i, row in enumerate(rows[1:], start=1):
                for k in range(1, 15):
                    v = text(row, k)
                    slug = snap.lookup.get(v.lower()) if v else None
                    if slug and slug in shorts and v != shorts[slug]:
                        fixes[a1(tab, i, k)] = [[shorts[slug]]]  # show the short code, as the website does
        if README in data and [text(r, 0) for r in data[README] if text(r, 0)] != \
                [r[0].strip() for r in README_TEXT if r[0].strip()]:
            self.sheet.clear([quote(README)])
            fixes[a1(README, 0, 0)] = README_TEXT  # keep the instructions current
        if fixes:
            self.sheet.write(fixes)
        prow = data.get(PCS, [])
        if header_ok(prow, HEADERS[PCS][:4]) and not text(prow[0], 4):
            self.sheet.write({a1(PCS, 0, 4): [["Server"]]})  # Sheets made before PCs had a server
        added = False
        for t in self.db.terms():
            tab = routine_tab(t["name"])
            if tab not in ids:
                self._add_routine(t, pcs)
                added = True
                continue
            rows = data.get(tab, [])
            if not header_ok(rows, ROUTINE_HEADER):
                continue
            listed = {text(r, 0).lower() for r in rows[1:]}
            missing = [p for p in pcs if p["name"].lower() not in listed and p["tag"] not in listed]
            if missing:
                self.sheet.write({a1(tab, len(rows), 0): [[p["name"]] for p in missing]})
        self._write_if_changed(PROBLEMS, self._problem_rows(snap.problems), "sheet_problems_sha")
        self._write_if_changed(ENROLLMENTS, self.enrollment_rows(), "sheet_enrolments_sha")
        try:
            self.style(data, None if added else ids)
            self.db.set_setting("sheet_style_error", "")
        except Exception as e:  # noqa: BLE001  (looks only: never let formatting fail a sync)
            log.warning("styling the Sheet failed: %s", e)
            self.db.set_setting("sheet_style_error", str(e)[:500])

    def style(self, data: dict | None = None, ids: dict | None = None, force: bool = False) -> bool:
        """Re-apply the Sheet's look when its tabs, groups or row counts changed. Returns True if it did."""
        ids = ids if ids is not None else self.sheet.tab_ids()
        if data is None:
            data = self.sheet.read([t for t in ids if t in HEADERS or t.startswith(ROUTINE)])
        groups = [{"slug": g["slug"], "short": g["short"] or short_code(g["slug"]), "color": g["color"]}
                  for g in self._groups_in_sheet_order(data)]
        routine = [t for t in ids if t.startswith(ROUTINE)]
        sig = hashlib.sha256(json.dumps([sorted(ids.items()), groups, {t: len(r) for t, r in data.items()},
                                         STYLE_VERSION], default=str).encode()).hexdigest()
        if not force and sig == self.db.setting("sheet_style_sha"):
            return False
        meta = self.sheet.meta()
        reqs = style_requests(meta, data, groups, {"readme": README, "groups": GROUPS, "pcs": PCS, "terms": TERMS,
                                                    "admins": ADMINS, "problems": PROBLEMS, "enrollments": ENROLLMENTS,
                                                    "routine": routine})
        reqs.append(_validation(ids[PCS], 1, 2, 3, {"type": "ONE_OF_LIST", "values": [
            {"userEnteredValue": "Yes"}, {"userEnteredValue": "No"}]}) if PCS in ids else None)
        reqs.append(_validation(ids[TERMS], 1, 1, 3, {"type": "DATE_IS_VALID"}) if TERMS in ids else None)
        reqs += [_routine_validation(ids[t]) for t in routine]
        self.sheet.batch_update([q for q in reqs if q])
        self.db.set_setting("sheet_style_sha", sig)
        return True

    def _groups_in_sheet_order(self, data: dict) -> list:
        by_slug = {g["slug"]: g for g in self.db.groups()}
        out = [by_slug[c] for c in (text(r, 0).lower() for r in data.get(GROUPS, [])[1:]) if c in by_slug]
        return out + [g for s, g in by_slug.items() if g not in out]

    def _problem_rows(self, problems: list[Problem]) -> list[list]:
        try:
            since = json.loads(self.db.setting("sheet_problem_since") or "{}")
        except ValueError:
            since = {}
        now = now_iso().replace("T", " ")
        since = {p.key(): since.get(p.key(), now) for p in problems}
        self.db.set_setting("sheet_problem_since", json.dumps(since))
        if not problems:
            return [HEADERS[PROBLEMS], ["No problems: everything in the Sheet is being enforced."]]
        return [HEADERS[PROBLEMS]] + [[p.where, p.value, p.problem, p.instead, since[p.key()]] for p in problems]

    def enrollment_rows(self) -> list[list]:
        rows = [HEADERS[ENROLLMENTS]]
        for r in self.db.q("SELECT id FROM enrolments ORDER BY id"):
            e = self.db.enrollment(r["id"])
            sups = [f"{s['name']} · {s['email']}" if s["email"] else s["name"] for s in e["supervisors"]]
            rows.append([e["id"], e["submitted_at"].replace("T", " "), e["status"], e["group_name"], e["thesis_title"],
                         sups[0] if sups else e["supervisor"], "\n".join(sups[1:]),
                         "\n".join(f"{m['name']} · {m['student_id']} · {m['email']}" for m in e["members"]),
                         e["hardware"], e["decided_by"] or "", e["decision_note"]])
        return rows

    def _write_if_changed(self, tab: str, rows: list[list], key: str) -> None:
        digest = hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()
        if digest == self.db.setting(key):
            return
        self.sheet.clear([quote(tab)])
        self.sheet.write({a1(tab, 0, 0): rows})
        self.db.set_setting(key, digest)

    # -- build / restore ----------------------------------------------------------------------
    def restore(self, actor: str) -> None:
        """Overwrite the Sheet with SQLite's last good copy."""
        with self.lock:
            self._build(self.sheet.tab_ids(), restore=True)
            self.db.set_setting("sheet_style_sha", "")
            self.db.audit(actor, "sheet-restored", "rewrote the Sheet from the last good copy")
        self.pull()

    def _build(self, ids: dict, restore: bool) -> None:
        db = self.db
        terms, pcs = db.terms(), db.pcs()
        members = db.members()
        tabs = [README, GROUPS, PCS, TERMS, ADMINS] + [routine_tab(t["name"]) for t in terms] + [PROBLEMS, ENROLLMENTS]
        new = [t for t in tabs if t not in ids]
        if new:
            self.sheet.batch_update([{"addSheet": {"properties": {"title": t}}} for t in new])
        ids = self.sheet.tab_ids()
        if restore:
            self.sheet.clear([quote(t) for t in tabs])
        else:
            spare = [sid for t, sid in ids.items() if t not in tabs and t.lower() in ("sheet1", "blad1", "feuille 1")]
            if spare:  # a brand-new spreadsheet's empty first tab
                self.sheet.batch_update([{"deleteSheet": {"sheetId": sid}} for sid in spare])
                ids = self.sheet.tab_ids()
        blocks = {
            a1(README, 0, 0): README_TEXT,
            a1(GROUPS, 0, 0): [HEADERS[GROUPS]] + [[g["slug"], g["name"], g["short"] or short_code(g["slug"]), g["color"],
                                                    "\n".join(members.get(g["id"], [])), g["notes"]]
                                                   for g in db.groups()],
            a1(PCS, 0, 0): [HEADERS[PCS]] + [[p["name"], p["tag"], "Yes" if p["ssh_enabled"] else "No", p["notes"],
                                              db.server_of(p)] for p in pcs],
            a1(ADMINS, 0, 0): [HEADERS[ADMINS]] + [[a] for a in db.admins()],
            a1(PROBLEMS, 0, 0): [HEADERS[PROBLEMS], ["No problems yet."]],
            a1(ENROLLMENTS, 0, 0): self.enrollment_rows(),
        }
        for t in terms:
            blocks[a1(routine_tab(t["name"]), 0, 0)] = self._routine_rows(t, pcs)
        self.sheet.write(blocks)
        # Dates as real dates (USER_ENTERED), so the Sheet shows a date picker.
        self.sheet.write({a1(TERMS, 0, 0): [HEADERS[TERMS]] + [[t["name"], t["start_date"], t["end_date"]]
                                                              for t in terms]}, raw=False)
        db.set_setting("sheet_problems_sha", "")
        db.set_setting("sheet_enrolments_sha", "")
        db.set_setting("sheet_style_sha", "")

    def _routine_rows(self, term, pcs) -> list[list]:
        cells = {(r["pc_id"], r["weekday"], r["half"]): r["short"] or short_code(r["slug"])
                 for r in self.db.bookings(term["id"], ("approved",))}
        return [ROUTINE_HEADER] + [[p["name"]] + [cells.get((p["id"], wd, h), "") for wd, h in SLOT_COLS]
                                   for p in pcs]

    def _add_routine(self, term, pcs) -> None:
        tab = routine_tab(term["name"])
        reply = self.sheet.batch_update([{"addSheet": {"properties": {"title": tab}}}])
        sid = reply["replies"][0]["addSheet"]["properties"]["sheetId"]
        self.sheet.write({a1(tab, 0, 0): self._routine_rows(term, pcs)})
        self.sheet.batch_update([_routine_validation(sid)])
        self.db.set_setting("sheet_style_sha", "")

    # -- website writes -----------------------------------------------------------------------
    def assign(self, group: dict, term_name: str, pc_tag: str, slots: list[tuple[int, str]]) -> SyncReport:
        """Add a group row and its routine cells, checked against the Sheet as it is now."""
        with self.lock:
            tab = routine_tab(term_name)
            try:
                data = self.sheet.read([GROUPS, PCS, tab])
            except SheetsError as e:
                raise AssignError(f"Could not read the Sheet: {e}") from e
            groups, routine = data.get(GROUPS, []), data.get(tab, [])
            if not header_ok(groups, HEADERS[GROUPS]) or not routine_header_ok(routine):
                raise AssignError(f"The Groups or {tab} tab has a changed header; fix it in the Sheet first.")
            if any(text(r, 0).lower() == group["slug"] for r in groups[1:]):
                raise AssignError(f"The Sheet already has a group with code {group['slug']}.")
            pc = self.db.one("SELECT * FROM pcs WHERE tag = ?", (pc_tag,))
            row_i = next((i for i, r in enumerate(routine) if i and text(r, 0).lower() in
                          (pc["name"].lower(), pc_tag)), None)
            blocks = {}
            if row_i is None:
                row_i = len(routine)
                blocks[a1(tab, row_i, 0)] = [[pc["name"]]]
            row = routine[row_i] if row_i < len(routine) else []
            taken = [f"{WEEKDAYS[wd]} {h} ({text(row, SLOT_COLS.index((wd, h)) + 1)})" for wd, h in slots
                     if text(row, SLOT_COLS.index((wd, h)) + 1)]
            if taken:
                raise AssignError("Already taken in the Sheet: " + ", ".join(taken) + ".")
            blocks[a1(GROUPS, len(groups), 0)] = [[group["slug"], group["name"], group["short"], group["color"],
                                                  "\n".join(group["members"]), group["notes"]]]
            for wd, h in slots:
                blocks[a1(tab, row_i, SLOT_COLS.index((wd, h)) + 1)] = [[group["short"] or short_code(group["slug"])]]
            self.sheet.write(blocks)
            return self._pull()


def _validation(sid: int, row0: int, col0: int, col1: int, condition: dict, strict: bool = True) -> dict:
    return {"setDataValidation": {
        "range": {"sheetId": sid, "startRowIndex": row0, "endRowIndex": 1000, "startColumnIndex": col0,
                  "endColumnIndex": col1},
        "rule": {"condition": condition, "strict": strict, "showCustomUi": True}}}


def _routine_validation(sid: int) -> dict:
    return _validation(sid, 1, 1, 15, {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": "=Groups!$C$2:$C"}]})
