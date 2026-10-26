"""Applies the schedule: policy, device clean-up and on-PC account switching.

reconcile() is idempotent and computes the desired state for "now" every
time, so a missed boundary (restart, outage) is corrected on the next run.
At a slot's end (SWITCH_WINDOW before the next slot starts) the slot is closed first: the policy drops
the outgoing group and its account is switched off. The incoming group is let in when its slot starts.
Order:
  1. write the policy for the current slot (fail closed to admins-only on error). Network access and
     Tailscale SSH logins change here: the PCs drop the outgoing group's sessions themselves.
  2. on each PC, make the incoming group's account the only slot-active one and stop the outgoing
     group's leftover processes (created on first use, so the incoming group can log in)
  3. remove devices of students who are not in an active slot (+ per-student cap), and of students who
     left every group a day ago or more. Last, because Headscale removes them one at a time and the
     policy already blocks them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .db import DB, now_iso
from .headscale import Headscale, Node
from .policy import PolicyInput, build_policy, render
from .slots import Slot, in_switch_window, next_slot, slot_at
from .sshexec import SSH

log = logging.getLogger("labslots.enforcer")
SWITCH_PARALLEL = 10  # PCs switched at once over SSH
FORMER_GRACE = timedelta(hours=24)  # a group cleared by mistake in the Sheet can be restored before this
FORMER_PER_RUN = 20  # leavers' devices removed per run (~1 s each), so a term's worth never delays a slot change


@dataclass
class Report:
    slot: Slot
    active: list[dict] = field(default_factory=list)
    policy_applied: bool = False
    policy_changed: bool = False
    admins_only: bool = False
    deleted: list[str] = field(default_factory=list)
    would_delete: list[str] = field(default_factory=list)
    ssh: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rotation: dict | None = None
    ssh_seconds: float = 0.0
    switching: bool = False  # in the switch window between two slots: no group has access


class Enforcer:
    def __init__(self, db: DB, hs: Headscale | None, ssh: SSH | None,
                 clock: Callable[[], datetime] = datetime.now):
        self.db = db
        self.hs = hs
        self.ssh = ssh
        self.clock = clock
        self.lock = threading.Lock()
        self.last: Report | None = None
        self.last_run: str = ""

    # -- test rotation ---------------------------------------------------------
    def rotation(self, now: datetime | None = None) -> dict | None:
        """A short test rotation (Settings): one PC cycles through groups every N seconds."""
        raw = self.db.setting("test_rotation")
        if not raw:
            return None
        try:
            r = json.loads(raw)
            start, end = datetime.fromisoformat(r["start"]), datetime.fromisoformat(r["end"])
        except (ValueError, KeyError, TypeError):
            return None
        now = now or self.clock()
        if now >= end:
            self.db.set_setting("test_rotation", "")
            self.db.audit("scheduler", "test-rotation-ended", f"ended {end.isoformat(timespec='seconds')}")
            return None
        if now < start or not r.get("groups"):
            return None
        elapsed = (now - start).total_seconds()
        idx = int(elapsed // r["seconds"])
        r["index"], r["group_id"] = idx, r["groups"][idx % len(r["groups"])]
        r["next_change"] = min(start + timedelta(seconds=(idx + 1) * r["seconds"]), end)
        r["end_dt"] = end
        return r

    def _rotation_booking(self, rot: dict) -> dict | None:
        row = self.db.one(
            "SELECT g.id AS group_id, g.slug, g.short, g.name AS group_name, g.color, g.linux_account AS account, "
            "p.id AS pc_id, p.tag AS pc_tag, p.name AS pc_name FROM groups g, pcs p "
            "WHERE g.id = ? AND p.id = ?", (rot["group_id"], rot["pc_id"]))
        return dict(row) | {"status": "approved", "test_rotation": True} if row else None

    # -- desired state ---------------------------------------------------------
    def active_bookings(self, slot: Slot, switching: bool = False) -> list[dict]:
        rows = [] if switching else [dict(r) for r in self.db.approved_on(slot.date, slot.weekday, slot.half)]
        rot = self.rotation()
        if rot:
            rows = [b for b in rows if b["pc_id"] != rot["pc_id"]]
            b = self._rotation_booking(rot)
            if b:
                rows.append(b)
        return rows

    def policy_input(self, active: list[dict]) -> PolicyInput:
        members = self.db.members()
        groups = {g["slug"]: [e.lower() for e in members.get(g["id"], [])] for g in self.db.groups()}
        return PolicyInput(
            admins=self.db.admins(),
            groups=groups,
            pc_tags=[p["tag"] for p in self.db.pcs()],
            active=[(b["slug"], b["pc_tag"]) for b in active],
            ports=json.loads(self.db.setting("slot_ports") or '["tcp:22"]'),
            extra_grants=json.loads(self.db.setting("extra_grants") or "[]"),
            extra_ssh=json.loads(self.db.setting("extra_ssh") or "[]"),
            accounts={b["slug"]: b["account"] for b in active},
        )

    def preview(self, when: datetime | None = None, admins_only: bool = False) -> str:
        when = when or self.clock()
        slot, switching = slot_at(when), in_switch_window(when)
        policy = build_policy(self.policy_input(self.active_bookings(slot, switching)), admins_only)
        return render(policy, self._header(slot, admins_only, switching))

    @staticmethod
    def _header(slot: Slot, admins_only: bool, switching: bool = False) -> str:
        mode = ("ADMINS-ONLY (fail closed)" if admins_only else
                f"switch-over: no group until {next_slot(slot).start:%H:%M}" if switching else "slot grants")
        return (f"Generated by labslots for {slot.label} ({mode}).\n"
                "Do not edit in Headplane: the scheduler rewrites this policy.\n"
                "Static rules live in labslots Settings -> Extra grants.")

    # -- reconcile -------------------------------------------------------------
    def reconcile(self, force: bool = False) -> Report:
        with self.lock:
            now = self.clock()
            slot = slot_at(now)
            rep = Report(slot=slot, switching=in_switch_window(now))
            rot = self.rotation(now)
            if rot:
                rep.rotation = {k: rot[k] for k in ("pc_id", "group_id", "index", "seconds", "next_change", "end_dt")}
            try:
                rep.active = self.active_bookings(slot, rep.switching)
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"reading schedule: {e}")
            self._apply_policy(rep, force)
            nodes = self._nodes(rep)
            self._switch_accounts(rep, nodes or [])
            if nodes is not None:
                self._cleanup_devices(rep, nodes)
            self.last, self.last_run = rep, now_iso()
            return rep

    def _apply_policy(self, rep: Report, force: bool) -> None:
        if not self.db.flag("enforce_policy") or self.hs is None:
            return
        try:
            if rep.errors:
                raise RuntimeError("; ".join(rep.errors))
            text = render(build_policy(self.policy_input(rep.active)), self._header(rep.slot, False, rep.switching))
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"building policy: {e}")
            rep.admins_only = True
            text = render(build_policy(self.policy_input([]), admins_only=True),
                          self._header(rep.slot, True))
        digest = hashlib.sha256(text.encode()).hexdigest()
        if not force and digest == self.db.setting("applied_policy_sha"):
            rep.policy_applied = True
            return
        try:
            self.hs.set_policy(text)
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"applying policy: {e}")
            if not rep.admins_only:
                rep.admins_only = True
                try:
                    fallback = render(build_policy(self.policy_input([]), admins_only=True),
                                      self._header(rep.slot, True))
                    self.hs.set_policy(fallback)
                    self.db.set_setting("applied_policy_sha", "")
                except Exception as e2:  # noqa: BLE001
                    rep.errors.append(f"applying admins-only fallback: {e2}")
            self.db.audit("scheduler", "policy-error", "; ".join(rep.errors))
            return
        rep.policy_applied = rep.policy_changed = True
        self.db.set_setting("applied_policy_sha", digest)
        self.db.set_setting("applied_policy_at", now_iso())
        who = ", ".join(f"{b['group_name']}->{b['pc_name']}" + (" [test]" if b.get("test_rotation") else "")
                        for b in rep.active) or "no groups"
        self.db.audit("scheduler", "policy-applied",
                      f"{rep.slot.label}{' switch-over' if rep.switching else ''}: {who}"
                      + (" [ADMINS-ONLY]" if rep.admins_only else ""))

    def _nodes(self, rep: Report) -> list[Node] | None:
        if self.hs is None:
            return None
        try:
            return self.hs.nodes()
        except Exception as e:  # noqa: BLE001
            rep.errors.append(f"listing nodes: {e}")
            return None

    def _cleanup_devices(self, rep: Report, nodes: list[Node]) -> None:
        """Delete personal devices of students outside their slot and beyond the cap (device_cleanup), and of
        students who left every group at least FORMER_GRACE ago (former_cleanup)."""
        admins = set(self.db.admins())
        members = self.db.members()
        students = {e.lower() for ems in members.values() for e in ems} - admins
        active_groups = {b["group_id"] for b in rep.active}
        entitled = {e.lower() for gid in active_groups for e in members.get(gid, [])}
        cap = int(self.db.setting("max_devices_per_student") or "2")
        cutoff = (self.clock() - FORMER_GRACE).isoformat(timespec="seconds")
        former = {r["email"] for r in self.db.q("SELECT email FROM former_members WHERE since <= ?", (cutoff,))}
        former -= admins | students

        doomed: list[tuple[Node, str, str]] = []  # (device, switch that allows removing it, label suffix)
        by_owner: dict[str, list[Node]] = {}
        leavers = 0
        for n in nodes:
            if n.tagged:
                continue  # lab PCs are never touched
            if n.owner in former:
                leavers += 1
                if leavers <= FORMER_PER_RUN or not self.db.flag("former_cleanup"):
                    doomed.append((n, "former_cleanup", ", in no group"))
            elif n.owner not in students:
                continue  # admins and people never on a roster are never touched
            elif n.owner not in entitled:
                doomed.append((n, "device_cleanup", ""))
            else:
                by_owner.setdefault(n.owner, []).append(n)
        for owner, devs in by_owner.items():
            devs.sort(key=lambda n: n.last_seen or n.created, reverse=True)
            doomed.extend((n, "device_cleanup", "") for n in devs[cap:])

        for n, switch, why in doomed:
            label = f"{n.name} ({n.owner}, id {n.id}{why})"
            if not self.db.flag(switch):
                rep.would_delete.append(label)
                continue
            try:
                self.hs.delete_node(n.id)
                rep.deleted.append(label)
                self.db.audit("scheduler", "device-removed", label)
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"removing {label}: {e}")

    def _switch_accounts(self, rep: Report, nodes: list[Node]) -> None:
        """Make each PC's slot-active account match the schedule. PCs are switched SWITCH_PARALLEL at a
        time, so a slot change on a big lab takes seconds, and an unreachable PC only delays itself."""
        if self.ssh is None or not self.db.flag("ssh_enforce"):
            return
        ip_by_tag: dict[str, str] = {}
        for n in nodes:
            for t in n.tags:
                ip_by_tag.setdefault(t, n.ipv4)
        want = {b["pc_id"]: b for b in rep.active}
        jobs = []
        for pc in self.db.pcs():
            if not pc["ssh_enabled"]:
                continue
            b = want.get(pc["id"])
            account = b["account"] if b else None
            state = self.db.one("SELECT * FROM pc_state WHERE pc_id = ?", (pc["id"],))
            target = account or "-"
            if state and state["active_account"] == target and not state["last_error"]:
                continue
            host = ip_by_tag.get(pc["tag"], "")
            if not host:
                msg = f"{pc['name']}: no online node with {pc['tag']}"
                rep.errors.append(msg)
                self._pc_state(pc["id"], state, error=msg)
                continue
            jobs.append((pc, state, host, account, target))
        if not jobs:
            return
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=SWITCH_PARALLEL) as pool:
            results = list(pool.map(lambda j: self.ssh.switch(j[2], j[3]), jobs))
        rep.ssh_seconds = round(time.monotonic() - t0, 1)
        for (pc, state, _host, _account, target), res in zip(jobs, results):
            if res.ok:
                rep.ssh.append(f"{pc['name']}: slot-active = {target}")
                self._pc_state(pc["id"], state, account=target)
                self.db.audit("scheduler", "pc-switched", f"{pc['name']}: {target} ({res.output})")
            else:
                msg = f"{pc['name']}: switch to {target} failed: {res.output}"
                rep.errors.append(msg)
                self._pc_state(pc["id"], state, error=msg)
                self.db.audit("scheduler", "pc-switch-error", msg)

    def _pc_state(self, pc_id: int, state, account: str | None = None, error: str | None = None):
        acc = account if account is not None else (state["active_account"] if state else None)
        self.db.x("INSERT INTO pc_state(pc_id, active_account, updated_at, last_error) "
                  "VALUES (?, ?, ?, ?) ON CONFLICT(pc_id) DO UPDATE SET "
                  "active_account = excluded.active_account, updated_at = excluded.updated_at, "
                  "last_error = excluded.last_error", (pc_id, acc, now_iso(), error))

    def next_change(self) -> datetime:
        rot = self.rotation()
        now = self.clock()
        slot = slot_at(now)
        normal = slot.end if now < slot.end else next_slot(slot).start
        return min(rot["next_change"], normal) if rot else normal

